"""API キーの保存(`scopus-tools config`)。

`uv tool install` した利用者にはリポジトリのクローンが無いので、
`~/.scopus-tools/.env` が唯一の安定した置き場所になる。鍵が平文で入るファイルなので、
権限を 600 に保つことがこのモジュールの主な責務。
"""
import os
import stat

import pytest

from scopus_tools import config


@pytest.fixture
def env_path(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    return config.user_env_path()


class TestLocation:
    def test_lives_beside_the_cache_and_projects(self, env_path, tmp_path):
        assert env_path == str(tmp_path / ".scopus-tools" / ".env")


class TestSetAndRead:
    def test_creates_the_file_and_directory(self, env_path):
        config.set_keys({"SCOPUS_API_KEY": "abc123"})
        assert config.read_env_file() == {"SCOPUS_API_KEY": "abc123"}

    def test_written_file_is_owner_only(self, env_path):
        config.set_keys({"SCOPUS_API_KEY": "abc123"})
        mode = stat.S_IMODE(os.stat(env_path).st_mode)
        assert mode & (stat.S_IRWXG | stat.S_IRWXO) == 0, "鍵が平文で入るので 600 であること"

    def test_merges_rather_than_replaces(self, env_path):
        config.set_keys({"SCOPUS_API_KEY": "abc"})
        config.set_keys({"KAKEN_APP_ID": "xyz"})
        assert config.read_env_file() == {"SCOPUS_API_KEY": "abc", "KAKEN_APP_ID": "xyz"}

    def test_overwrites_an_existing_value(self, env_path):
        config.set_keys({"SCOPUS_API_KEY": "old"})
        config.set_keys({"SCOPUS_API_KEY": "new"})
        assert config.read_env_file()["SCOPUS_API_KEY"] == "new"

    def test_keeps_unrelated_keys(self, env_path):
        """このファイルを専有しない(他のツールの行を消さない)。"""
        os.makedirs(os.path.dirname(env_path), exist_ok=True)
        with open(env_path, "w") as f:
            f.write("OTHER_TOOL_KEY=keepme\n")
        config.set_keys({"SCOPUS_API_KEY": "abc"})
        assert config.read_env_file()["OTHER_TOOL_KEY"] == "keepme"

    def test_unknown_key_is_rejected(self, env_path):
        with pytest.raises(ValueError, match="unknown key"):
            config.set_keys({"SCOPUS_APIKEY": "typo"})
        assert not os.path.exists(env_path), "拒否したら書き込まない"

    def test_empty_value_is_rejected(self, env_path):
        with pytest.raises(ValueError, match="empty value"):
            config.set_keys({"SCOPUS_API_KEY": "   "})


class TestReadFormats:
    def test_ignores_comments_and_blank_lines(self, env_path):
        os.makedirs(os.path.dirname(env_path), exist_ok=True)
        with open(env_path, "w") as f:
            f.write("# comment\n\nSCOPUS_API_KEY=abc\n")
        assert config.read_env_file() == {"SCOPUS_API_KEY": "abc"}

    def test_accepts_export_and_quotes(self, env_path):
        os.makedirs(os.path.dirname(env_path), exist_ok=True)
        with open(env_path, "w") as f:
            f.write('export SCOPUS_API_KEY="abc"\nKAKEN_APP_ID=\'xyz\'\n')
        assert config.read_env_file() == {"SCOPUS_API_KEY": "abc", "KAKEN_APP_ID": "xyz"}

    def test_missing_file_reads_as_empty(self, env_path):
        assert config.read_env_file() == {}


class TestUnset:
    def test_removes_only_the_named_key(self, env_path):
        config.set_keys({"SCOPUS_API_KEY": "abc", "KAKEN_APP_ID": "xyz"})
        assert config.unset_keys(["KAKEN_APP_ID"]) == ["KAKEN_APP_ID"]
        assert config.read_env_file() == {"SCOPUS_API_KEY": "abc"}

    def test_absent_key_is_not_an_error(self, env_path):
        assert config.unset_keys(["KAKEN_APP_ID"]) == []


class TestMasking:
    @pytest.mark.parametrize("value,expected", [
        ("abcdef123456", "********3456"),
        ("abcd", "****"),
        ("ab", "**"),
        ("", ""),
    ])
    def test_only_the_last_four_survive(self, value, expected):
        assert config.mask(value) == expected

    def test_describe_never_returns_the_raw_value(self, env_path):
        config.set_keys({"SCOPUS_API_KEY": "supersecretvalue"})
        info = config.describe()
        assert "supersecretvalue" not in str(info)
        assert info["keys"]["SCOPUS_API_KEY"].endswith("alue")


class TestParseAssignments:
    def test_key_value_pairs(self):
        assigned, prompt = config.parse_assignments(["SCOPUS_API_KEY=abc"])
        assert assigned == {"SCOPUS_API_KEY": "abc"} and prompt == []

    def test_bare_key_is_flagged_for_prompting(self):
        """コマンドラインに鍵を書かせないための経路。"""
        assigned, prompt = config.parse_assignments(["SCOPUS_API_KEY"])
        assert assigned == {} and prompt == ["SCOPUS_API_KEY"]

    def test_key_with_empty_value_also_prompts(self):
        assigned, prompt = config.parse_assignments(["SCOPUS_API_KEY="])
        assert prompt == ["SCOPUS_API_KEY"]

    def test_value_containing_equals_is_kept_whole(self):
        assigned, _ = config.parse_assignments(["SCOPUS_API_KEY=a=b=c"])
        assert assigned["SCOPUS_API_KEY"] == "a=b=c"


class TestDescribe:
    def test_reports_missing_keys(self, env_path):
        config.set_keys({"SCOPUS_API_KEY": "abc"})
        assert config.describe()["missing"] == ["KAKEN_APP_ID", "WOS_API_KEY"]

    def test_flags_world_readable(self, env_path):
        config.set_keys({"SCOPUS_API_KEY": "abc"})
        os.chmod(env_path, 0o644)
        assert config.describe()["world_readable"] is True
