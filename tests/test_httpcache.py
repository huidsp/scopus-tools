"""httpcache.HttpLayer のユニットテスト。実ネットワークには触れない。"""
import sqlite3
import time
from unittest.mock import patch

import pytest

from conftest import make_response
from scopus_tools import cachedb, httpcache
from scopus_tools.httpcache import (
    HttpLayer, OfflineError, QuotaExceeded, RateLimited, cache_key, canonical_params,
)

URL = "https://api.elsevier.com/content/search/scopus"
SECRET = "super-secret-api-key-12345"


@pytest.fixture
def layer(cache_db):
    return HttpLayer(db=cache_db, auth_params={"apiKey": SECRET}, enabled=True, max_retries=0)


class TestKeyDerivation:
    def test_secrets_are_excluded(self):
        assert "apiKey" not in canonical_params({"query": "x", "apiKey": SECRET})
        assert "appid" not in canonical_params({"query": "x", "appid": "abc"})
        assert "insttoken" not in canonical_params({"insttoken": "t"})

    def test_key_ignores_secret_values(self):
        """キーがローテーションしてもキャッシュが無効化されないこと。"""
        a = cache_key("GET", URL, {"query": "x", "apiKey": "key-one"})
        b = cache_key("GET", URL, {"query": "x", "apiKey": "key-two"})
        assert a == b

    def test_key_is_order_independent(self):
        a = cache_key("GET", URL, {"query": "x", "start": 0})
        b = cache_key("GET", URL, {"start": 0, "query": "x"})
        assert a == b

    def test_key_varies_with_params_and_accept(self):
        base = cache_key("GET", URL, {"query": "x"}, "application/json")
        assert base != cache_key("GET", URL, {"query": "y"}, "application/json")
        assert base != cache_key("GET", URL, {"query": "x"}, "application/xml")
        assert base != cache_key("GET", URL + "/other", {"query": "x"}, "application/json")

    def test_empty_values_dropped(self):
        assert canonical_params({"a": "", "b": None, "c": 0}) == {"c": "0"}


class TestSecretLeak:
    def test_api_key_never_reaches_disk(self, cache_db, layer):
        """DB のどこにも API キー文字列が現れないこと(最重要)。"""
        with patch("requests.get", return_value=make_response({"ok": 1})) as get_mock:
            layer.get(URL, {"query": "AU-ID(1)"}, headers={"Accept": "application/json"},
                      api="scopus_search")

        # 送信時には確かに付いている
        assert get_mock.call_args.kwargs["params"]["apiKey"] == SECRET

        conn = sqlite3.connect(cache_db.path)
        dump = "\n".join(conn.iterdump())
        conn.close()
        assert SECRET not in dump
        assert "apiKey" not in dump

    def test_api_key_never_reaches_logs_even_at_debug(self, cache_db, caplog):
        """urllib3 は DEBUG で完全な URL を出す。Scopus は apiKey をクエリで渡すので
        抑えないと鍵がログに残り、MCP の stderr はクライアントがファイルに保存する。"""
        import logging

        layer = HttpLayer(db=cache_db, auth_params={"apiKey": SECRET}, enabled=True)
        with caplog.at_level(logging.DEBUG):
            with patch("requests.get", return_value=make_response({"ok": 1})):
                layer.get(URL, {"query": "AU-ID(1)"}, api="scopus_search")
        assert SECRET not in caplog.text

    def test_url_logging_libraries_are_clamped(self):
        import logging

        from scopus_tools import utils
        logging.getLogger("urllib3").setLevel(logging.DEBUG)   # わざと緩める
        utils.silence_url_logging()
        for name in ("urllib3", "requests", "httpx", "httpcore"):
            assert logging.getLogger(name).level == logging.WARNING

    def test_appid_never_reaches_disk(self, cache_db):
        layer = HttpLayer(db=cache_db, auth_params={"appid": SECRET}, enabled=True)
        with patch("requests.get", return_value=make_response({"ok": 1})):
            layer.get("https://nrid.nii.ac.jp/opensearch/", {"qg": "x"},
                      api="kaken_researcher")
        conn = sqlite3.connect(cache_db.path)
        dump = "\n".join(conn.iterdump())
        conn.close()
        assert SECRET not in dump


class TestCaching:
    def test_hit_avoids_network(self, layer):
        with patch("requests.get", return_value=make_response({"n": 1})) as get_mock:
            first = layer.get(URL, {"query": "x"}, api="scopus_search")
            assert get_mock.call_count == 1
        with patch("requests.get") as get_mock2:
            second = layer.get(URL, {"query": "x"}, api="scopus_search")
        get_mock2.assert_not_called()
        assert second.cached is True and first.cached is False
        assert second.json() == {"n": 1}
        assert second.fetched_at == first.fetched_at

    def test_non_200_is_not_cached(self, layer, cache_db):
        with patch("requests.get", return_value=make_response(status_code=404)):
            res = layer.get(URL, {"query": "x"}, api="scopus_search")
        assert res.status_code == 404
        assert cache_db.stats()["entries"] == 0

    def test_refresh_bypasses_and_overwrites(self, layer):
        with patch("requests.get", return_value=make_response({"v": 1})):
            layer.get(URL, {"query": "x"}, api="scopus_search")
        with patch("requests.get", return_value=make_response({"v": 2})) as get_mock:
            res = layer.get(URL, {"query": "x"}, api="scopus_search", refresh=True)
        assert get_mock.call_count == 1
        assert res.cached is False and res.json() == {"v": 2}
        with patch("requests.get") as get_mock2:
            again = layer.get(URL, {"query": "x"}, api="scopus_search")
        get_mock2.assert_not_called()
        assert again.json() == {"v": 2}      # 上書きされている

    def test_disabled_layer_never_caches(self, cache_db):
        layer = HttpLayer(db=cache_db, auth_params={"apiKey": SECRET}, enabled=False)
        with patch("requests.get", return_value=make_response({"n": 1})) as get_mock:
            layer.get(URL, {"query": "x"}, api="scopus_search")
            layer.get(URL, {"query": "x"}, api="scopus_search")
        assert get_mock.call_count == 2
        assert cache_db.stats()["entries"] == 0

    def test_different_params_are_separate_entries(self, layer, cache_db):
        with patch("requests.get", return_value=make_response({"n": 1})):
            layer.get(URL, {"query": "x", "start": 0}, api="scopus_search")
            layer.get(URL, {"query": "x", "start": 25}, api="scopus_search")
        assert cache_db.stats()["entries"] == 2

    def test_xml_body_survives_roundtrip(self, layer):
        xml = b"<?xml version='1.0'?><grantAwards><grantAward/></grantAwards>"
        with patch("requests.get", return_value=make_response(body=xml)):
            layer.get(URL, {"q": "1"}, api="kaken_project")
        with patch("requests.get"):
            hit = layer.get(URL, {"q": "1"}, api="kaken_project")
        assert hit.content == xml


class TestOffline:
    def test_offline_hit_works(self, cache_db):
        warm = HttpLayer(db=cache_db, auth_params={"apiKey": SECRET}, enabled=True)
        with patch("requests.get", return_value=make_response({"n": 1})):
            warm.get(URL, {"query": "x"}, api="scopus_search")

        offline = HttpLayer(db=cache_db, auth_params={"apiKey": SECRET},
                            enabled=True, offline=True)
        with patch("requests.get") as get_mock:
            res = offline.get(URL, {"query": "x"}, api="scopus_search")
        get_mock.assert_not_called()
        assert res.cached is True

    def test_offline_miss_raises(self, cache_db):
        offline = HttpLayer(db=cache_db, auth_params={"apiKey": SECRET},
                            enabled=True, offline=True)
        with patch("requests.get") as get_mock:
            with pytest.raises(OfflineError):
                offline.get(URL, {"query": "nope"}, api="scopus_search")
        get_mock.assert_not_called()


class TestRateLimits:
    def test_headers_are_recorded(self, layer, cache_db):
        resp = make_response({"n": 1}, headers={
            "X-RateLimit-Limit": "20000", "X-RateLimit-Remaining": "19998",
            "X-RateLimit-Reset": "1800000000"})
        with patch("requests.get", return_value=resp):
            layer.get(URL, {"query": "x"}, api="scopus_search")
        row = cache_db.get_rate_limit("scopus_search")
        assert row["limit_total"] == 20000 and row["remaining"] == 19998

    def test_quota_exceeded_fails_fast(self, layer, cache_db):
        reset = int(time.time()) + 4 * 86400
        resp = make_response(status_code=429, headers={
            "X-ELS-Status": "QUOTA_EXCEEDED", "X-RateLimit-Reset": str(reset)})
        with patch("requests.get", return_value=resp) as get_mock, patch("time.sleep") as slept:
            with pytest.raises(QuotaExceeded) as exc:
                layer.get(URL, {"query": "x"}, api="scopus_author_search")
        assert get_mock.call_count == 1        # リトライしない
        slept.assert_not_called()              # 数日待つ sleep は絶対にしない
        assert exc.value.reset_at == reset
        assert cache_db.get_rate_limit("scopus_author_search")["quota_blocked"] == 1

    def test_known_quota_block_skips_network(self, layer, cache_db):
        cache_db.record_rate_limit("scopus_author_search", status=429, quota_blocked=True,
                                   reset_ts=int(time.time()) + 3600)
        with patch("requests.get") as get_mock:
            with pytest.raises(QuotaExceeded):
                layer.get(URL, {"query": "y"}, api="scopus_author_search")
        get_mock.assert_not_called()

    def test_expired_quota_block_is_cleared(self, layer, cache_db):
        cache_db.record_rate_limit("scopus_search", status=429, quota_blocked=True,
                                   reset_ts=int(time.time()) - 10)
        with patch("requests.get", return_value=make_response({"n": 1})) as get_mock:
            res = layer.get(URL, {"query": "y"}, api="scopus_search")
        assert get_mock.call_count == 1 and res.status_code == 200
        assert cache_db.get_rate_limit("scopus_search")["quota_blocked"] == 0

    def test_throttle_429_is_retried(self, cache_db):
        layer = HttpLayer(db=cache_db, auth_params={"apiKey": SECRET},
                          enabled=True, max_retries=2)
        resp = make_response(status_code=429)      # X-ELS-Status なし = 秒間超過
        with patch("requests.get", return_value=resp) as get_mock, patch("time.sleep"):
            with pytest.raises(RateLimited):
                layer.get(URL, {"query": "x"}, api="scopus_search")
        assert get_mock.call_count == 3            # 初回 + リトライ 2 回

    def test_retry_after_is_honoured(self, cache_db):
        layer = HttpLayer(db=cache_db, auth_params={"apiKey": SECRET},
                          enabled=True, max_retries=1)
        resp = make_response(status_code=429, headers={"Retry-After": "7"})
        with patch("requests.get", return_value=resp), patch("time.sleep") as slept:
            with pytest.raises(RateLimited):
                layer.get(URL, {"query": "x"}, api="scopus_search")
        # サーバの指示どおり 7 秒待つ(指数バックオフの 1 秒ではない)
        waits = [c[0][0] for c in slept.call_args_list if c[0] and c[0][0] > 0.5]
        assert 7.0 in waits

    def test_retry_after_is_capped_by_max_wait(self, cache_db):
        layer = HttpLayer(db=cache_db, auth_params={"apiKey": SECRET},
                          enabled=True, max_retries=1, max_wait=3.0)
        resp = make_response(status_code=429, headers={"Retry-After": "600"})
        with patch("requests.get", return_value=resp), patch("time.sleep") as slept:
            with pytest.raises(RateLimited):
                layer.get(URL, {"query": "x"}, api="scopus_search")
        assert all(c[0][0] <= 3.0 for c in slept.call_args_list if c[0])

    def test_backoff_is_capped(self, cache_db):
        layer = HttpLayer(db=cache_db, auth_params={"apiKey": SECRET},
                          enabled=True, max_retries=6, max_wait=5.0)
        assert all(layer._backoff(i) <= 5.0 for i in range(10))


class TestThrottle:
    def test_min_interval_enforced(self, cache_db):
        layer = HttpLayer(db=cache_db, auth_params={"apiKey": SECRET}, enabled=False)
        with patch("requests.get", return_value=make_response({"n": 1})), \
             patch("time.sleep") as slept:
            layer.get(URL, {"query": "a"}, api="scopus_author_search")  # 2 req/s
            layer.get(URL, {"query": "b"}, api="scopus_author_search")
        # 2 回目は 1/2*1.2 = 0.6 秒ぶん待たされる
        assert slept.called
        assert slept.call_args[0][0] > 0

    def test_concurrent_requests_reserve_distinct_slots(self, cache_db):
        """並列ページ取得でも rps を守る: 予約制なので送信時刻が重ならない。

        2 スレッドが同時に _throttle を通っても、片方は min_interval ぶん
        待たされる(同じ last を読んで一斉送信、が起きない)。
        """
        import threading
        layer = HttpLayer(db=cache_db, auth_params={"apiKey": SECRET}, enabled=False)
        waits = []
        with patch("requests.get", return_value=make_response({"n": 1})), \
             patch("time.sleep", side_effect=lambda w: waits.append(w)):
            threads = [threading.Thread(
                target=lambda q: layer.get(URL, {"query": q}, api="scopus_author_search"),
                args=(str(i),)) for i in range(3)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        # 3 リクエスト中 2 つは待たされ、待ち時間は互いに異なる(スロットが直列)
        assert len(waits) >= 2
        assert len(set(round(w, 3) for w in waits)) == len(waits)

    def test_worker_thread_fetches_land_in_parent_collect(self, cache_db):
        """snapshot/adopt_collectors でワーカーの取得が親の collect() に載る。"""
        import threading
        layer = HttpLayer(db=cache_db, auth_params={"apiKey": SECRET}, enabled=False)
        with patch("requests.get", return_value=make_response({"n": 1})), \
             patch("time.sleep"):
            with layer.collect() as records:
                snapshot = layer.snapshot_collectors()

                def work():
                    layer.adopt_collectors(snapshot)
                    layer.get(URL, {"query": "w"}, api="scopus_search")

                t = threading.Thread(target=work)
                t.start()
                t.join()
        assert len(records) == 1
        assert records[0]["api"] == "scopus_search"

    def test_cache_hits_do_not_throttle(self, layer):
        with patch("requests.get", return_value=make_response({"n": 1})):
            layer.get(URL, {"query": "a"}, api="scopus_author_search")
        with patch("time.sleep") as slept, patch("requests.get"):
            layer.get(URL, {"query": "a"}, api="scopus_author_search")
        slept.assert_not_called()


class TestTransport:
    def test_timeout_is_always_passed(self, layer):
        with patch("requests.get", return_value=make_response({"n": 1})) as get_mock:
            layer.get(URL, {"query": "x"}, api="scopus_search")
        assert get_mock.call_args.kwargs["timeout"] == httpcache.DEFAULT_TIMEOUT

    def test_session_is_used_when_given(self, cache_db):
        from unittest.mock import MagicMock
        session = MagicMock()
        session.get.return_value = make_response({"n": 1})
        layer = HttpLayer(db=cache_db, session=session, auth_params={"apiKey": SECRET},
                          enabled=True)
        with patch("requests.get") as bare:
            layer.get(URL, {"query": "x"}, api="scopus_search")
        bare.assert_not_called()
        assert session.get.call_count == 1
        assert session.get.call_args.kwargs["timeout"] == httpcache.DEFAULT_TIMEOUT

    def test_connection_error_is_retried(self, cache_db):
        import requests as _requests
        layer = HttpLayer(db=cache_db, auth_params={"apiKey": SECRET},
                          enabled=True, max_retries=2)
        with patch("requests.get", side_effect=_requests.ConnectionError("boom")) as get_mock, \
             patch("time.sleep"):
            with pytest.raises(_requests.ConnectionError):
                layer.get(URL, {"query": "x"}, api="scopus_search")
        assert get_mock.call_count == 3

    def test_timeout_recovers_on_retry(self, cache_db):
        import requests as _requests
        layer = HttpLayer(db=cache_db, auth_params={"apiKey": SECRET},
                          enabled=True, max_retries=2)
        with patch("requests.get",
                   side_effect=[_requests.Timeout("slow"), make_response({"n": 1})]), \
             patch("time.sleep"):
            res = layer.get(URL, {"query": "x"}, api="scopus_search")
        assert res.status_code == 200


class TestHttpResult:
    def test_text_and_json(self):
        from scopus_tools.httpcache import HttpResult
        r = HttpResult(200, b'{"a": 1}', {}, False, "now", URL)
        assert r.json() == {"a": 1}
        assert r.text == '{"a": 1}'

    def test_text_survives_bad_encoding(self):
        """kaken.py が resp.text[:300] をエラー経路で使うので落ちてはいけない。"""
        from scopus_tools.httpcache import HttpResult
        r = HttpResult(500, b"\xff\xfe invalid", {}, False, "now", URL, encoding="utf-8")
        assert isinstance(r.text, str)
