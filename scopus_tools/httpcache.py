"""HTTP の継ぎ目: タイムアウト・スロットル・429 リトライ・SQLite キャッシュ。

`ScopusClient` と `KakenClient` の唯一の送信経路。両者で 1 回だけ実装する。

**秘密情報をディスクに書かない**ための三重防御:
  1. `apiKey` / `appid` は `HttpLayer(auth_params=...)` が保持し、キャッシュキーと
     `params_json` を算出した**後**に注入する。呼び出し側の params に秘密が入らない
  2. `canonical_params()` が既知の秘密キー名を denylist で除去する
  3. `tests/test_httpcache.py` が「DB のどこにも API キー文字列が現れない」ことを表明する

**stdout を汚さない**: ログはすべて logging(= stderr)経由。MCP は stdout が
JSON-RPC 専用なので、ここから print してはいけない。
"""

import contextlib
import hashlib
import json
import logging
import os
import random
import threading
import time

import requests

from scopus_tools import cachedb, utils

logger = logging.getLogger(__name__)

# 既知の秘密パラメータ名。キャッシュキーにも params_json にも含めない。
SECRET_PARAM_NAMES = ("apikey", "api_key", "appid", "app_id", "insttoken", "token")

# 保存するレスポンスヘッダ(ホワイトリスト)。Set-Cookie 等は決して保存しない。
HEADER_WHITELIST = (
    "x-ratelimit-limit", "x-ratelimit-remaining", "x-ratelimit-reset",
    "x-els-status", "x-els-reqid", "content-type", "date", "etag",
    "retry-after",   # 429 の待ち時間判断に使うので必須
)

# Elsevier 公表のクォータとスロットリング。kaken は公表値が無いので控えめに 1 req/s。
API_LIMITS = {
    "scopus_search":           {"rps": 9, "weekly": 20000},
    "scopus_author_search":    {"rps": 2, "weekly": 5000},
    "scopus_author_retrieval": {"rps": 3, "weekly": 5000},
    "kaken_project":           {"rps": 1, "weekly": None},
    "kaken_researcher":        {"rps": 1, "weekly": None},
    # Web of Science Starter。契約機関(Institutional)は 5 req/s・5,000 req/日。
    # 実測したレスポンスヘッダ X-RateLimit-Remaining-Day / -Second で確認できる。
    # weekly は「週」枠なのでここでは None にし、日次はサーバ側の 429 に任せる。
    "wos_documents":           {"rps": 5, "weekly": None},
}

DEFAULT_TIMEOUT = (10, 60)   # (connect, read)
RETRY_STATUSES = (500, 502, 503, 504)


class QuotaExceeded(Exception):
    """週クォータ枯渇(429 + X-ELS-Status: QUOTA_EXCEEDED)。リトライしても無駄。"""

    def __init__(self, api, reset_at=None):
        self.api = api
        self.reset_at = reset_at
        super().__init__(self._message())

    @property
    def reset_at_text(self):
        """リセット時刻を「2026-08-20 15:23 (in 2d 23h)」形式で。不明なら None。

        `reset_at` は epoch 秒。数値以外が来ても例外にしない — クォータ枯渇の
        報告経路そのものが落ちると、本当の原因が見えなくなる。
        """
        try:
            reset = float(self.reset_at)
        except (TypeError, ValueError):
            return None
        remain = max(0, int(reset - time.time()))
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(reset))
        return f"{when} (in {remain // 86400}d {(remain % 86400) // 3600}h)"

    def _message(self):
        # ベンダ名は API 種別から決める(KAKEN は NII で、Elsevier ではない)。
        vendor = "NII" if self.api.startswith("kaken") else "Elsevier"
        when = self.reset_at_text
        if not when:
            return f"{self.api}: {vendor} quota exhausted."
        return f"{self.api}: {vendor} quota exhausted until {when}."


class RateLimited(Exception):
    """秒間スロットル超過が規定回数のリトライ後も解消しなかった。"""


class OfflineError(Exception):
    """--offline なのにキャッシュに無いリクエストが必要になった。"""


def canonical_params(params):
    """キャッシュキー / 保存用にパラメータを正規化する(秘密は除去)。"""
    out = {}
    for k, v in (params or {}).items():
        if k.lower() in SECRET_PARAM_NAMES:
            continue
        if v is None or v == "":
            continue
        out[str(k)] = str(v)
    return dict(sorted(out.items()))


def cache_key(method, url, params, accept=""):
    blob = json.dumps([method.upper(), url, canonical_params(params), accept or ""],
                      ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _int_or_none(value):
    """ヘッダ値を int に。MagicMock や不正値でも例外を出さない。"""
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _pick_headers(headers):
    """ホワイトリストのヘッダだけを小文字キーの dict にする。"""
    out = {}
    try:
        items = headers.items()
    except (AttributeError, TypeError):
        return out
    for k, v in items:
        try:
            lk = str(k).lower()
        except Exception:      # pragma: no cover
            continue
        if lk in HEADER_WHITELIST:
            out[lk] = str(v)
    return out


class HttpResult:
    """requests.Response の代わりに返す軽量な結果オブジェクト。

    `.json()` / `.text` / `.content` / `.status_code` を提供する
    (既存の呼び出し側がこの 4 つを使っているため)。
    """

    __slots__ = ("status_code", "content", "headers", "cached", "fetched_at", "url", "encoding")

    def __init__(self, status_code, content, headers, cached, fetched_at, url, encoding=None):
        self.status_code = status_code
        self.content = content or b""
        self.headers = headers or {}
        self.cached = cached
        self.fetched_at = fetched_at
        self.url = url
        self.encoding = encoding or "utf-8"

    @property
    def text(self):
        try:
            return self.content.decode(self.encoding, errors="replace")
        except (LookupError, AttributeError):
            return self.content.decode("utf-8", errors="replace")

    def json(self):
        return json.loads(self.text)

    def __repr__(self):     # pragma: no cover
        return (f"<HttpResult {self.status_code} cached={self.cached} "
                f"fetched_at={self.fetched_at} {self.url}>")


def _raw_get(session, url, params, headers, timeout):
    """実際の送信。**モジュールレベル関数のまま置くこと**。

    session=None なら `requests.get` に落ちるので、`requests.get` をパッチしている
    既存テストがそのまま通る。本番は CLI / MCP が Session を渡してコネクションを再利用する。
    """
    if session is not None:
        return session.get(url, params=params, headers=headers, timeout=timeout)
    return requests.get(url, params=params, headers=headers, timeout=timeout)


class HttpLayer:
    def __init__(self, *, db=None, session=None, auth_params=None, auth_headers=None,
                 timeout=DEFAULT_TIMEOUT, max_retries=3, max_wait=60.0,
                 refresh=False, offline=False, enabled=None):
        self.db = db
        self.session = session
        self.auth_params = dict(auth_params or {})
        # ヘッダで鍵を渡す API 用(Web of Science は X-ApiKey ヘッダ)。params と同じく
        # キャッシュキー算出後に注入する。リクエストヘッダはキャッシュキーにも
        # DB にも入らない(保存するのは HEADER_WHITELIST のレスポンスヘッダだけ)ので、
        # これで鍵がディスクに落ちる経路は無い。
        self.auth_headers = dict(auth_headers or {})
        self.timeout = timeout
        self.max_retries = max_retries
        self.max_wait = max_wait
        self.refresh = refresh
        self.offline = offline
        # enabled=None なら環境変数で判断
        self.enabled = (not cachedb.cache_disabled()) if enabled is None else bool(enabled)
        self._last_request_at = {}     # api -> monotonic
        self._collectors = threading.local()
        # ここから先は auth_params(= API キー)を載せたリクエストを送る。
        # urllib3 等は DEBUG で完全な URL を出すので、CLI を経由しない
        # ライブラリ利用でも鍵が漏れないようにここで抑えておく。
        utils.silence_url_logging()

    # ---- 取得メタデータの収集 ------------------------------------------

    @contextlib.contextmanager
    def collect(self):
        """このブロック内で行った取得の (api, fetched_at, cached) を集める。

        `search_papers` のようにページングする操作でも、構成した全リクエストの
        取得日時が取れるので、操作全体の as-of 日付を「最も古い fetched_at」として
        導出できる。スレッドごとに独立(MCP のツール呼び出しが並行しても混ざらない)。
        """
        stack = getattr(self._collectors, "stack", None)
        if stack is None:
            stack = self._collectors.stack = []
        records = []
        stack.append(records)
        try:
            yield records
        finally:
            stack.pop()

    def _record_fetch(self, api, fetched_at, cached):
        stack = getattr(self._collectors, "stack", None)
        if not stack:
            return
        entry = {"api": api, "fetched_at": fetched_at, "cached": cached}
        for records in stack:
            records.append(entry)

    # ---- スロットル ---------------------------------------------------

    def _throttle(self, api):
        rps = (API_LIMITS.get(api) or {}).get("rps")
        if not rps:
            return
        min_interval = (1.0 / rps) * 1.2      # 1.2 は安全係数
        last = self._last_request_at.get(api)
        now = time.monotonic()
        if last is not None:
            wait = min_interval - (now - last)
            if wait > 0:
                time.sleep(wait)
        self._last_request_at[api] = time.monotonic()

    # ---- クォータ状態 -------------------------------------------------

    def _quota_blocked_until(self, api):
        """既知のクォータ枯渇中なら reset の epoch 秒を返す。"""
        if self.db is None:
            return None
        row = self.db.get_rate_limit(api)
        if not row or not row.get("quota_blocked"):
            return None
        reset = row.get("reset_ts")
        if reset and time.time() >= reset:
            self.db.clear_quota_block(api)
            return None
        return reset or 0

    def _record_headers(self, api, headers, status):
        if self.db is None:
            return
        limit = _int_or_none(headers.get("x-ratelimit-limit"))
        remaining = _int_or_none(headers.get("x-ratelimit-remaining"))
        reset = _int_or_none(headers.get("x-ratelimit-reset"))
        self.db.record_rate_limit(api, limit=limit, remaining=remaining,
                                  reset_ts=reset, status=status)
        if limit and remaining is not None and remaining < max(1, limit * 0.05):
            logger.warning("%s quota nearly exhausted: %d/%d remaining", api, remaining, limit)

    # ---- 本体 ---------------------------------------------------------

    def get(self, url, params=None, *, headers=None, api="generic", refresh=None):
        params = dict(params or {})
        headers = dict(headers or {})
        accept = headers.get("Accept", "")
        use_refresh = self.refresh if refresh is None else bool(refresh)
        key = cache_key("GET", url, params, accept)
        safe_params = canonical_params(params)

        # 1. キャッシュ
        if self.enabled and self.db is not None and not use_refresh:
            hit = self.db.get_http(key)
            if hit is not None:
                logger.debug("Cache hit (%s): %s", api, safe_params)
                self._record_fetch(api, hit["fetched_at"], True)
                return HttpResult(hit["status"], hit["body"], hit["headers"],
                                  True, hit["fetched_at"], url, hit["encoding"])

        if self.offline:
            raise OfflineError(
                f"--offline: {api} response is not cached "
                f"(url={url}, params={safe_params}). Re-run without --offline to fetch it.")

        # 2. 既知のクォータ枯渇なら即失敗(死んだクォータに追加消費しない)
        blocked_until = self._quota_blocked_until(api)
        if blocked_until is not None:
            raise QuotaExceeded(api, blocked_until or None)

        # 3. 送信(リトライ付き)
        send_params = dict(params)
        send_params.update(self.auth_params)   # 秘密はここで初めて混ざる
        send_headers = dict(headers or {})
        send_headers.update(self.auth_headers)
        return self._send(url, send_params, send_headers, api, key, safe_params)

    def _send(self, url, send_params, headers, api, key, safe_params):
        attempt = 0
        while True:
            self._throttle(api)
            try:
                resp = _raw_get(self.session, url, send_params, headers, self.timeout)
            except (requests.Timeout, requests.ConnectionError) as e:
                if attempt >= self.max_retries:
                    raise
                delay = self._backoff(attempt)
                logger.warning("%s: %s — retrying in %.1fs (%d/%d)",
                               api, type(e).__name__, delay, attempt + 1, self.max_retries)
                time.sleep(delay)
                attempt += 1
                continue

            status = getattr(resp, "status_code", None)
            resp_headers = _pick_headers(getattr(resp, "headers", None))
            self._record_headers(api, resp_headers, status)

            if status == 429:
                els_status = (resp_headers.get("x-els-status") or "").upper()
                reset = _int_or_none(resp_headers.get("x-ratelimit-reset"))
                if "QUOTA" in els_status:
                    # 週クォータ枯渇。リトライも sleep もせず即座に失敗させる。
                    if self.db is not None:
                        self.db.record_rate_limit(api, reset_ts=reset, status=429,
                                                  quota_blocked=True)
                    raise QuotaExceeded(api, reset)
                if attempt >= self.max_retries:
                    raise RateLimited(f"{api}: throttled after {attempt} retries")
                delay = self._retry_after(resp_headers, attempt)
                logger.warning("%s: throttled (429) — retrying in %.1fs (%d/%d)",
                               api, delay, attempt + 1, self.max_retries)
                time.sleep(delay)
                attempt += 1
                continue

            if status in RETRY_STATUSES and attempt < self.max_retries:
                delay = self._backoff(attempt)
                logger.warning("%s: HTTP %s — retrying in %.1fs (%d/%d)",
                               api, status, delay, attempt + 1, self.max_retries)
                time.sleep(delay)
                attempt += 1
                continue

            return self._finish(resp, status, resp_headers, api, key, safe_params, url)

    def _finish(self, resp, status, resp_headers, api, key, safe_params, url):
        body = getattr(resp, "content", b"") or b""
        if not isinstance(body, (bytes, bytearray)):
            body = str(body).encode("utf-8")
        encoding = getattr(resp, "encoding", None) or "utf-8"
        if not isinstance(encoding, str):
            encoding = "utf-8"
        fetched_at = None
        if status == 200 and self.enabled and self.db is not None:
            # put_http が保存した fetched_at をそのまま使う。読み直すと
            # hits カウンタが書き込み時に増えてしまう。
            fetched_at = self.db.put_http(
                key, api=api, url=url, params=safe_params, status=status,
                headers=resp_headers, body=bytes(body), encoding=encoding)
        if fetched_at is None:
            fetched_at = cachedb._now_iso()
        if status == 200:
            self._record_fetch(api, fetched_at, False)
        return HttpResult(status, bytes(body), resp_headers, False, fetched_at, url, encoding)

    def _backoff(self, attempt):
        return min(self.max_wait, (2 ** attempt) * (1.0 + random.random() * 0.3))

    def _retry_after(self, resp_headers, attempt):
        retry_after = _int_or_none(resp_headers.get("retry-after"))
        if retry_after is not None:
            return min(self.max_wait, float(retry_after))
        return self._backoff(attempt)

    def close(self):
        if self.session is not None:
            try:
                self.session.close()
            except Exception:      # pragma: no cover
                pass


class HttpContext:
    """キャッシュ DB と Session を 1 つだけ持ち、クライアント別の HttpLayer を払い出す。

    Scopus と KAKEN は認証パラメータが違うのでレイヤは分けるが、DB とコネクション
    プールは共有する。スロットル状態はレイヤごとだが、API ファミリが重ならないので問題ない。
    """

    def __init__(self, *, db=None, session=None, timeout=DEFAULT_TIMEOUT,
                 refresh=False, offline=False, enabled=True):
        self.db = db
        self.session = session
        self.timeout = timeout
        self.refresh = refresh
        self.offline = offline
        self.enabled = enabled

    def layer_for(self, auth_params=None, auth_headers=None):
        return HttpLayer(db=self.db, session=self.session, auth_params=auth_params,
                         auth_headers=auth_headers,
                         timeout=self.timeout, refresh=self.refresh,
                         offline=self.offline, enabled=self.enabled)

    def close(self):
        if self.session is not None:
            try:
                self.session.close()
            except Exception:      # pragma: no cover
                pass
        if self.db is not None:
            self.db.close()


def build_context(*, cache_db=None, no_cache=False, refresh=False, offline=False,
                  timeout=None, use_session=True):
    """CLI / MCP から使う HttpContext のファクトリ。

    `no_cache=True` または $SCOPUS_TOOLS_CACHE_DISABLE のときは DB を開かない
    (キャッシュファイルにも触れない)。
    """
    enabled = not (no_cache or cachedb.cache_disabled())
    db = cachedb.CacheDB(cache_db) if enabled else None

    session = None
    if use_session:
        session = requests.Session()
        adapter = requests.adapters.HTTPAdapter(
            pool_connections=4, pool_maxsize=8, max_retries=0)
        session.mount("https://", adapter)
        session.mount("http://", adapter)

    if timeout is None:
        env_timeout = os.getenv("SCOPUS_TOOLS_TIMEOUT")
        if env_timeout:
            try:
                timeout = float(env_timeout)
            except ValueError:
                logger.warning("Ignoring invalid SCOPUS_TOOLS_TIMEOUT=%r", env_timeout)
    if timeout is None:
        timeout = DEFAULT_TIMEOUT

    return HttpContext(db=db, session=session, timeout=timeout,
                       refresh=refresh, offline=offline, enabled=enabled)
