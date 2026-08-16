"""cachedb.CacheDB のユニットテスト。ネットワークには触れない。"""
import os
import sqlite3
import time

import pytest

from scopus_tools import cachedb


def _put(db, key="k1", api="scopus_search", body=b'{"ok":1}', status=200):
    return db.put_http(key, api=api, url="https://example.test/x",
                       params={"query": "AU-ID(1)"}, status=status,
                       headers={"content-type": "application/json"},
                       body=body, encoding="utf-8")


class TestSchema:
    def test_connect_is_idempotent(self, tmp_path):
        path = str(tmp_path / "c.sqlite3")
        for _ in range(3):
            conn = cachedb.connect(path)
            conn.close()
        conn = cachedb.connect(path)
        version = conn.execute("SELECT value FROM schema_meta WHERE key='version'").fetchone()[0]
        assert int(version) == cachedb.SCHEMA_VERSION
        conn.close()

    def test_wal_mode_enabled(self, tmp_path):
        conn = cachedb.connect(str(tmp_path / "c.sqlite3"))
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode.lower() == "wal"
        conn.close()

    def test_creates_parent_directory(self, tmp_path):
        path = str(tmp_path / "nested" / "deeper" / "c.sqlite3")
        conn = cachedb.connect(path)
        conn.close()
        assert os.path.exists(path)

    def test_non_200_rejected_by_check_constraint(self, tmp_path):
        """「失敗はキャッシュしない」をスキーマレベルで強制していること。"""
        conn = cachedb.connect(str(tmp_path / "c.sqlite3"))
        with pytest.raises(sqlite3.IntegrityError):
            with conn:
                conn.execute(
                    "INSERT INTO http_responses "
                    "(key, api, url, params_json, status, headers_json, body, encoding, "
                    " fetched_at, fetched_ts) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    ("k", "a", "u", "{}", 500, "{}", b"x", "utf-8", "now", 0))
        conn.close()

    def test_version_mismatch_recreates_db(self, tmp_path, caplog):
        path = str(tmp_path / "c.sqlite3")
        conn = cachedb.connect(path)
        with conn:
            conn.execute("UPDATE schema_meta SET value='999' WHERE key='version'")
            conn.execute(
                "INSERT INTO http_responses "
                "(key, api, url, params_json, status, headers_json, body, encoding, "
                " fetched_at, fetched_ts) VALUES (?,?,?,?,?,?,?,?,?,?)",
                ("old", "a", "u", "{}", 200, "{}", b"x", "utf-8", "now", 0))
        conn.close()

        db = cachedb.CacheDB(path)
        assert db.get_http("old") is None          # 作り直されている
        assert os.path.exists(path + ".bak")       # 旧ファイルは退避されている
        assert _put(db)                            # 新しい DB は使える
        db.close()


class TestHttpEntries:
    def test_roundtrip(self, cache_db):
        assert _put(cache_db)      # 保存した fetched_at を返す
        got = cache_db.get_http("k1")
        assert got["body"] == b'{"ok":1}'
        assert got["api"] == "scopus_search"
        assert got["headers"]["content-type"] == "application/json"
        assert got["fetched_at"]

    def test_miss_returns_none(self, cache_db):
        assert cache_db.get_http("nope") is None

    def test_put_ignores_non_200(self, cache_db):
        assert _put(cache_db, key="bad", status=500) is None
        assert cache_db.get_http("bad") is None

    def test_hits_are_counted(self, cache_db):
        _put(cache_db)
        assert cache_db.stats()["hits"] == 0     # 書き込みでヒットは増えない
        for _ in range(3):
            cache_db.get_http("k1")
        assert cache_db.stats()["hits"] == 3

    def test_put_is_idempotent(self, cache_db):
        _put(cache_db, body=b"first")
        _put(cache_db, body=b"second")
        assert cache_db.get_http("k1")["body"] == b"second"
        assert cache_db.stats()["entries"] == 1

    def test_delete(self, cache_db):
        _put(cache_db)
        assert cache_db.delete_http("k1") == 1
        assert cache_db.get_http("k1") is None


class TestRateLimits:
    def test_record_and_read(self, cache_db):
        cache_db.record_rate_limit("scopus_search", limit=20000, remaining=19999, status=200)
        row = cache_db.get_rate_limit("scopus_search")
        assert row["limit_total"] == 20000 and row["remaining"] == 19999

    def test_partial_update_keeps_previous_values(self, cache_db):
        cache_db.record_rate_limit("scopus_search", limit=20000, remaining=100, status=200)
        # 429 時は Limit/Remaining が返らない。既存値を消してはいけない
        cache_db.record_rate_limit("scopus_search", reset_ts=12345, status=429, quota_blocked=True)
        row = cache_db.get_rate_limit("scopus_search")
        assert row["limit_total"] == 20000
        assert row["remaining"] == 100
        assert row["reset_ts"] == 12345
        assert row["quota_blocked"] == 1

    def test_clear_quota_block(self, cache_db):
        cache_db.record_rate_limit("s", quota_blocked=True, status=429)
        cache_db.clear_quota_block("s")
        assert cache_db.get_rate_limit("s")["quota_blocked"] == 0


class TestMaintenance:
    def test_stats_per_api(self, cache_db):
        _put(cache_db, key="a", api="scopus_search")
        _put(cache_db, key="b", api="scopus_search")
        _put(cache_db, key="c", api="kaken_project")
        stats = cache_db.stats()
        assert stats["entries"] == 3
        counts = {r["api"]: r["n"] for r in stats["per_api"]}
        assert counts == {"scopus_search": 2, "kaken_project": 1}

    def test_prune_by_api(self, cache_db):
        _put(cache_db, key="a", api="scopus_search")
        _put(cache_db, key="c", api="kaken_project")
        assert cache_db.prune(api="scopus_search") == 1
        assert cache_db.get_http("c") is not None

    def test_prune_by_age(self, cache_db):
        _put(cache_db, key="old")
        _put(cache_db, key="new")
        conn = cachedb.connect(cache_db.path)
        with conn:
            conn.execute("UPDATE http_responses SET fetched_ts = ? WHERE key='old'",
                         (int(time.time()) - 40 * 86400,))
        conn.close()
        assert cache_db.prune(older_than_days=30) == 1
        assert cache_db.get_http("new") is not None

    def test_list_entries(self, cache_db):
        _put(cache_db, key="a")
        rows = cache_db.list_entries()
        assert rows[0]["key"] == "a" and rows[0]["size"] > 0


class TestDegradation:
    """キャッシュの失敗が取得を壊してはいけない。"""

    def test_unwritable_path_degrades_quietly(self, tmp_path):
        blocker = tmp_path / "blocker"
        blocker.write_text("not a directory")
        db = cachedb.CacheDB(str(blocker / "sub" / "c.sqlite3"))
        assert _put(db) is None        # 例外ではなく None
        assert db.get_http("k1") is None
        assert db.stats()["entries"] == 0

    def test_corrupt_db_is_recreated(self, tmp_path):
        path = tmp_path / "c.sqlite3"
        path.write_bytes(b"this is definitely not a sqlite file" * 10)
        db = cachedb.CacheDB(str(path))
        assert _put(db)                 # 作り直して使える
        assert db.get_http("k1") is not None
        db.close()


class TestPaths:
    def test_env_overrides_default_db_path(self, monkeypatch):
        monkeypatch.setenv("SCOPUS_TOOLS_CACHE_DB", "/tmp/custom.sqlite3")
        assert cachedb.default_db_path() == "/tmp/custom.sqlite3"

    def test_default_path_under_state_dir(self, monkeypatch):
        monkeypatch.delenv("SCOPUS_TOOLS_CACHE_DB", raising=False)
        assert cachedb.default_db_path().startswith(cachedb.default_state_dir())

    def test_projects_dir_shares_state_dir(self, monkeypatch):
        from scopus_tools import projects
        assert projects.default_projects_dir().startswith(cachedb.default_state_dir())

    @pytest.mark.parametrize("value,expected", [
        ("1", True), ("true", True), ("yes", True),
        ("0", False), ("", False), ("false", False),
    ])
    def test_cache_disabled_flag(self, monkeypatch, value, expected):
        monkeypatch.setenv("SCOPUS_TOOLS_CACHE_DISABLE", value)
        assert cachedb.cache_disabled() is expected
