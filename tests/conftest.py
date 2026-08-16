"""テスト全体を密閉するための設定。

キャッシュを既定で無効化し、DB とホームディレクトリを tmp_path に向ける。これにより:
  - テストが実ユーザの ~/.scopus-tools を汚さない
  - あるテストが書いたキャッシュ 200 を別のテストが食う事故が起きない
    (例: test_http_error_returns_empty が前のテストのキャッシュを引くと偽陽性になる)

キャッシュ自体を検証するテストは `cache_db` fixture で明示的に有効化する。
"""
import json
from unittest.mock import MagicMock

import pytest

from scopus_tools import cachedb


def make_response(payload=None, status_code=200, *, body=None, headers=None):
    """requests.Response 相当のモック。

    キャッシュ層は生バイト列を保存するので、`.json()` だけでなく **`.content` も**
    本物と同じように埋める。`.headers` は本物の dict にする(MagicMock の
    `.items()` は反復できないため)。
    """
    response = MagicMock()
    response.status_code = status_code
    if body is None:
        body = b"" if payload is None else json.dumps(payload).encode("utf-8")
    response.content = body
    response.encoding = "utf-8"
    response.text = body.decode("utf-8", errors="replace")
    response.json.return_value = payload if payload is not None else {}
    response.headers = dict(headers or {})
    return response


@pytest.fixture
def response_factory():
    return make_response


@pytest.fixture(autouse=True)
def _hermetic_cache(monkeypatch, tmp_path):
    monkeypatch.setenv("SCOPUS_TOOLS_CACHE_DISABLE", "1")
    monkeypatch.setenv("SCOPUS_TOOLS_CACHE_DB", str(tmp_path / "cache.sqlite3"))
    monkeypatch.setenv("HOME", str(tmp_path))
    yield


@pytest.fixture
def cache_db(monkeypatch, tmp_path):
    """キャッシュを有効にした CacheDB を返す(キャッシュ層自体のテスト用)。"""
    monkeypatch.delenv("SCOPUS_TOOLS_CACHE_DISABLE", raising=False)
    db = cachedb.CacheDB(str(tmp_path / "cache.sqlite3"))
    yield db
    db.close()
