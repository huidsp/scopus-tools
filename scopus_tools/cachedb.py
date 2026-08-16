"""SQLite によるレスポンスキャッシュの永続化層。HTTP のことは何も知らない。

Scopus API のクォータ(Author Search は週 5,000 件と最も厳しい)を節約するため、
成功した GET レスポンスをそのまま保存する。**キャッシュは HTTP リクエスト単位**で、
「操作(ページング全体)単位」では保存しない —— ページ 2 だけ失敗した取得は
ページ 1・3 だけが残り、次回はページ 2 のみ取りに行って自己修復する。

保存先の既定は `~/.scopus-tools/cache.sqlite3`(プロジェクト JSON の兄弟)。
優先順位: 明示パス > $SCOPUS_TOOLS_CACHE_DB > 既定。
$SCOPUS_TOOLS_CACHE_DISABLE=1 でキャッシュ自体を無効化する。

**キャッシュの失敗が取得を壊してはいけない。** DB がロックされていても壊れていても、
ログを出してキャッシュ無しで処理を続ける(`CacheDB` の各メソッドは例外を投げない)。
"""

import datetime
import json
import logging
import os
import sqlite3
import threading
import time

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS http_responses (
  key          TEXT PRIMARY KEY,
  api          TEXT NOT NULL,
  url          TEXT NOT NULL,
  params_json  TEXT NOT NULL,
  status       INTEGER NOT NULL,
  headers_json TEXT NOT NULL,
  body         BLOB NOT NULL,
  encoding     TEXT,
  fetched_at   TEXT NOT NULL,
  fetched_ts   INTEGER NOT NULL,
  hits         INTEGER NOT NULL DEFAULT 0,
  last_hit_ts  INTEGER,
  CHECK (status = 200)
);

CREATE INDEX IF NOT EXISTS ix_http_api_ts ON http_responses(api, fetched_ts);

CREATE TABLE IF NOT EXISTS rate_limits (
  api           TEXT PRIMARY KEY,
  limit_total   INTEGER,
  remaining     INTEGER,
  reset_ts      INTEGER,
  quota_blocked INTEGER NOT NULL DEFAULT 0,
  last_status   INTEGER,
  observed_ts   INTEGER NOT NULL
);
"""


def default_state_dir():
    """既定の状態ディレクトリ: ~/.scopus-tools/"""
    return os.path.expanduser("~/.scopus-tools")


def default_db_path():
    """キャッシュ DB のパス。環境変数 SCOPUS_TOOLS_CACHE_DB があればそちらを優先。"""
    env = os.getenv("SCOPUS_TOOLS_CACHE_DB")
    if env:
        return env
    return os.path.join(default_state_dir(), "cache.sqlite3")


def cache_disabled():
    """$SCOPUS_TOOLS_CACHE_DISABLE でキャッシュが無効化されているか。"""
    return os.getenv("SCOPUS_TOOLS_CACHE_DISABLE", "").strip() not in ("", "0", "false", "False")


def _now_iso():
    return datetime.datetime.now().isoformat(timespec="seconds")


def connect(path):
    """DB を開き、PRAGMA とスキーマを適用した接続を返す(冪等)。"""
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(path, timeout=5.0)
    conn.row_factory = sqlite3.Row
    # WAL は CLI と MCP サーバが同じ DB に同時アクセスするために必要。
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.executescript(_SCHEMA)
    _check_version(conn, path)
    return conn


def _check_version(conn, path):
    row = conn.execute("SELECT value FROM schema_meta WHERE key='version'").fetchone()
    if row is None:
        with conn:
            conn.execute("INSERT OR REPLACE INTO schema_meta(key, value) VALUES('version', ?)",
                         (str(SCHEMA_VERSION),))
        return
    try:
        found = int(row["value"])
    except (TypeError, ValueError):
        found = -1
    if found != SCHEMA_VERSION:
        # キャッシュは再生成できるので、移行でユーザを止めない。
        raise _SchemaMismatch(found)


class _SchemaMismatch(Exception):
    def __init__(self, found):
        super().__init__(f"cache schema version {found} != {SCHEMA_VERSION}")
        self.found = found


class CacheDB:
    """スレッドごとに接続を持つ、例外を投げないキャッシュストア。

    どのメソッドも失敗時はログを出して None / 0 を返す。呼び出し側は
    「キャッシュが無かった」として扱えばよい。
    """

    def __init__(self, path=None):
        self.path = path or default_db_path()
        self._local = threading.local()
        self._broken = False

    # ---- 接続管理 --------------------------------------------------

    def _conn(self):
        if self._broken:
            return None
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            return conn
        try:
            conn = connect(self.path)
        except _SchemaMismatch as e:
            self._reset_db(reason=str(e))
            try:
                conn = connect(self.path)
            except Exception as e2:      # pragma: no cover - 二重失敗は諦める
                logger.warning("Cache disabled (cannot open %s after reset): %s", self.path, e2)
                self._broken = True
                return None
        except sqlite3.DatabaseError as e:
            self._reset_db(reason=f"corrupt database: {e}")
            try:
                conn = connect(self.path)
            except Exception as e2:      # pragma: no cover
                logger.warning("Cache disabled (cannot open %s): %s", self.path, e2)
                self._broken = True
                return None
        except OSError as e:
            logger.warning("Cache disabled (cannot create %s): %s", self.path, e)
            self._broken = True
            return None
        self._local.conn = conn
        return conn

    def _reset_db(self, reason):
        """壊れた / 版違いの DB を退避して作り直す。キャッシュは再生成可能。"""
        backup = f"{self.path}.bak"
        logger.warning("Recreating cache DB (%s). Old file moved to %s", reason, backup)
        try:
            if os.path.exists(self.path):
                os.replace(self.path, backup)
            for suffix in ("-wal", "-shm"):
                side = self.path + suffix
                if os.path.exists(side):
                    os.remove(side)
        except OSError as e:              # pragma: no cover
            logger.warning("Could not move aside the old cache DB: %s", e)

    def _safe(self, fn, default=None):
        """DB 操作を包み、ロック / エラーで処理を止めない。"""
        conn = self._conn()
        if conn is None:
            return default
        for attempt in range(3):
            try:
                return fn(conn)
            except sqlite3.OperationalError as e:
                if "locked" in str(e).lower() or "busy" in str(e).lower():
                    time.sleep(0.1 * (attempt + 1))
                    continue
                logger.warning("Cache operation failed: %s", e)
                return default
            except sqlite3.DatabaseError as e:
                logger.warning("Cache operation failed: %s", e)
                return default
        logger.warning("Cache operation gave up after lock contention")
        return default

    def close(self):
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:         # pragma: no cover
                pass
            self._local.conn = None

    # ---- HTTP レスポンス --------------------------------------------

    def get_http(self, key):
        """キャッシュ済みレスポンスを dict で返す。無ければ None。ヒット数を加算する。"""
        def _op(conn):
            row = conn.execute("SELECT * FROM http_responses WHERE key = ?", (key,)).fetchone()
            if row is None:
                return None
            with conn:
                conn.execute(
                    "UPDATE http_responses SET hits = hits + 1, last_hit_ts = ? WHERE key = ?",
                    (int(time.time()), key))
            return {
                "key": row["key"], "api": row["api"], "url": row["url"],
                "status": row["status"], "body": row["body"],
                "encoding": row["encoding"],
                "headers": json.loads(row["headers_json"]),
                "params": json.loads(row["params_json"]),
                "fetched_at": row["fetched_at"], "fetched_ts": row["fetched_ts"],
            }
        return self._safe(_op)

    def put_http(self, key, *, api, url, params, status, headers, body, encoding=None):
        """成功レスポンスを保存し、保存した `fetched_at` を返す(失敗時は None)。

        status が 200 以外なら何もしない。200 以外はスキーマの CHECK でも弾かれるが、
        ここでも早期に返して「失敗を保存しない」意図をコード上でも明示する。
        """
        if status != 200:
            return None
        now = int(time.time())
        fetched_at = _now_iso()

        def _op(conn):
            with conn:
                conn.execute(
                    "INSERT OR REPLACE INTO http_responses "
                    "(key, api, url, params_json, status, headers_json, body, encoding, "
                    " fetched_at, fetched_ts, hits, last_hit_ts) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,0,NULL)",
                    (key, api, url,
                     json.dumps(params, ensure_ascii=False, sort_keys=True),
                     status,
                     json.dumps(headers, ensure_ascii=False, sort_keys=True),
                     body, encoding, fetched_at, now))
            return fetched_at
        return self._safe(_op, default=None)

    def delete_http(self, key):
        def _op(conn):
            with conn:
                cur = conn.execute("DELETE FROM http_responses WHERE key = ?", (key,))
            return cur.rowcount
        return self._safe(_op, default=0)

    # ---- レート制限 --------------------------------------------------

    def record_rate_limit(self, api, *, limit=None, remaining=None, reset_ts=None,
                          status=None, quota_blocked=None):
        """X-RateLimit-* ヘッダの観測結果を記録する。None の項目は既存値を維持する。"""
        def _op(conn):
            prev = conn.execute("SELECT * FROM rate_limits WHERE api = ?", (api,)).fetchone()
            merged = {
                "limit_total": limit if limit is not None else (prev["limit_total"] if prev else None),
                "remaining": remaining if remaining is not None else (prev["remaining"] if prev else None),
                "reset_ts": reset_ts if reset_ts is not None else (prev["reset_ts"] if prev else None),
                "quota_blocked": int(quota_blocked) if quota_blocked is not None
                                 else (prev["quota_blocked"] if prev else 0),
                "last_status": status if status is not None else (prev["last_status"] if prev else None),
            }
            with conn:
                conn.execute(
                    "INSERT OR REPLACE INTO rate_limits "
                    "(api, limit_total, remaining, reset_ts, quota_blocked, last_status, observed_ts) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (api, merged["limit_total"], merged["remaining"], merged["reset_ts"],
                     merged["quota_blocked"], merged["last_status"], int(time.time())))
            return True
        return bool(self._safe(_op, default=False))

    def get_rate_limit(self, api):
        def _op(conn):
            row = conn.execute("SELECT * FROM rate_limits WHERE api = ?", (api,)).fetchone()
            return dict(row) if row is not None else None
        return self._safe(_op)

    def clear_quota_block(self, api):
        def _op(conn):
            with conn:
                conn.execute("UPDATE rate_limits SET quota_blocked = 0 WHERE api = ?", (api,))
            return True
        return bool(self._safe(_op, default=False))

    # ---- 運用 --------------------------------------------------------

    def stats(self):
        """件数・容量・API 別内訳・最古/最新・クォータ状態を返す。"""
        def _op(conn):
            total = conn.execute(
                "SELECT COUNT(*) n, COALESCE(SUM(LENGTH(body)),0) b, "
                "       COALESCE(SUM(hits),0) h, MIN(fetched_ts) mn, MAX(fetched_ts) mx "
                "FROM http_responses").fetchone()
            per_api = [dict(r) for r in conn.execute(
                "SELECT api, COUNT(*) n, COALESCE(SUM(hits),0) hits, "
                "       MIN(fetched_ts) oldest_ts, MAX(fetched_ts) newest_ts "
                "FROM http_responses GROUP BY api ORDER BY n DESC")]
            limits = [dict(r) for r in conn.execute("SELECT * FROM rate_limits")]
            return {
                "path": self.path,
                "entries": total["n"],
                "body_bytes": total["b"],
                "hits": total["h"],
                "oldest_ts": total["mn"],
                "newest_ts": total["mx"],
                "per_api": per_api,
                "rate_limits": limits,
            }
        return self._safe(_op, default={"path": self.path, "entries": 0, "body_bytes": 0,
                                        "hits": 0, "oldest_ts": None, "newest_ts": None,
                                        "per_api": [], "rate_limits": []})

    def list_entries(self, *, api=None, older_than_days=None, limit=100):
        def _op(conn):
            sql = ("SELECT key, api, url, params_json, fetched_at, fetched_ts, hits, "
                   "       LENGTH(body) size FROM http_responses WHERE 1=1")
            args = []
            if api:
                sql += " AND api = ?"
                args.append(api)
            if older_than_days is not None:
                sql += " AND fetched_ts < ?"
                args.append(int(time.time() - older_than_days * 86400))
            sql += " ORDER BY fetched_ts DESC LIMIT ?"
            args.append(int(limit))
            return [dict(r) for r in conn.execute(sql, args)]
        return self._safe(_op, default=[])

    def prune(self, *, api=None, older_than_days=None):
        """条件に合うエントリを削除して件数を返す。条件なしなら全削除。"""
        def _op(conn):
            sql = "DELETE FROM http_responses WHERE 1=1"
            args = []
            if api:
                sql += " AND api = ?"
                args.append(api)
            if older_than_days is not None:
                sql += " AND fetched_ts < ?"
                args.append(int(time.time() - older_than_days * 86400))
            with conn:
                cur = conn.execute(sql, args)
            return cur.rowcount
        return self._safe(_op, default=0)

    def vacuum(self):
        def _op(conn):
            conn.execute("VACUUM")
            return True
        return bool(self._safe(_op, default=False))
