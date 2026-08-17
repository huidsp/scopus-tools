"""mcp_setup: MCP クライアントへの登録。

実際に人が詰まるのは「絶対パスが要る」「シェルの export が届かない」の 2 点なので、
そこを外していないかを固定する。設定ファイルを壊さないことも重要な保証。
"""
import json
import os
import stat
from unittest.mock import patch

import pytest

from scopus_tools import mcp_setup


@pytest.fixture
def cfg(tmp_path):
    return str(tmp_path / "claude_desktop_config.json")


def _entry(**kw):
    kw.setdefault("with_keys", False)
    return mcp_setup.build_entry(**kw)


class TestResolveCommand:
    def test_returns_absolute_path(self):
        command, _args = mcp_setup.resolve_command()
        assert os.path.isabs(command), "MCP クライアントは PATH を引き継がない"

    def test_symlink_is_resolved_to_the_real_file(self, tmp_path):
        """uv tool install は symlink を張るので実体まで解決する。"""
        real = tmp_path / "real-scopus-tools"
        real.write_text("#!/bin/sh\n")
        real.chmod(0o755)
        link = tmp_path / "scopus-tools"
        link.symlink_to(real)

        with patch.object(mcp_setup.sys, "argv", [str(link)]):
            command, args = mcp_setup.resolve_command()
        assert command == str(real.resolve())
        assert args == []

    def test_falls_back_to_python_module(self, tmp_path):
        with patch.object(mcp_setup.sys, "argv", [str(tmp_path / "gone")]), \
             patch("shutil.which", return_value=None):
            command, args = mcp_setup.resolve_command()
        assert os.path.isabs(command)
        assert args == ["-m", "scopus_tools.cli"]


class TestBuildEntry:
    def test_paths_are_absolutised(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        os.makedirs("index", exist_ok=True)
        entry = _entry(scie_dir="index")
        assert os.path.isabs(entry["args"][entry["args"].index("--scie-dir") + 1])

    def test_keys_are_embedded_by_default(self):
        entry = mcp_setup.build_entry(with_keys=True,
                                      env={"SCOPUS_API_KEY": "abc", "KAKEN_APP_ID": "def"})
        assert entry["env"] == {"SCOPUS_API_KEY": "abc", "KAKEN_APP_ID": "def"}

    def test_no_keys_omits_env(self):
        entry = mcp_setup.build_entry(with_keys=False, env={"SCOPUS_API_KEY": "abc"})
        assert "env" not in entry

    def test_missing_keys_are_simply_absent(self):
        entry = mcp_setup.build_entry(with_keys=True, env={"SCOPUS_API_KEY": "abc"})
        assert entry["env"] == {"SCOPUS_API_KEY": "abc"}

    def test_args_start_with_mcp(self):
        entry = _entry()
        assert entry["args"][0] == "mcp" or entry["args"][:3] == ["-m", "scopus_tools.cli", "mcp"]


class TestClaudeDesktopRegistration:
    def test_creates_file_when_absent(self, cfg):
        res = mcp_setup.register_claude_desktop(_entry(), path=cfg)
        assert res["replaced"] is False
        data = json.loads(open(cfg).read())
        assert "scopus" in data["mcpServers"]

    def test_preserves_other_servers_and_top_level_keys(self, cfg):
        existing = {
            "globalShortcut": "Cmd+Space",
            "someOtherSetting": {"a": 1},
            "mcpServers": {"other-server": {"command": "/bin/true", "args": []}},
        }
        with open(cfg, "w") as f:
            json.dump(existing, f)

        mcp_setup.register_claude_desktop(_entry(), path=cfg)
        data = json.loads(open(cfg).read())
        assert data["globalShortcut"] == "Cmd+Space"
        assert data["someOtherSetting"] == {"a": 1}
        assert "other-server" in data["mcpServers"]
        assert "scopus" in data["mcpServers"]

    def test_takes_a_backup(self, cfg):
        with open(cfg, "w") as f:
            json.dump({"mcpServers": {}}, f)
        res = mcp_setup.register_claude_desktop(_entry(), path=cfg)
        assert os.path.exists(res["backup"])

    def test_reregistration_updates_in_place(self, cfg):
        mcp_setup.register_claude_desktop(_entry(), path=cfg)
        res = mcp_setup.register_claude_desktop(_entry(scie_dir="."), path=cfg)
        assert res["replaced"] is True
        data = json.loads(open(cfg).read())
        assert len(data["mcpServers"]) == 1

    def test_written_file_is_owner_only(self, cfg):
        """API キーが入りうるので 600 で書く。"""
        mcp_setup.register_claude_desktop(
            mcp_setup.build_entry(with_keys=True, env={"SCOPUS_API_KEY": "s3cret"}),
            path=cfg)
        mode = stat.S_IMODE(os.stat(cfg).st_mode)
        assert mode & (stat.S_IRWXG | stat.S_IRWXO) == 0

    def test_key_loss_is_detected_and_rolled_back(self, cfg):
        """書き込みで既存キーが消えたら .bak から戻して中止する。"""
        original = {"keepMe": 1, "mcpServers": {"other": {"command": "/bin/true"}}}
        with open(cfg, "w") as f:
            json.dump(original, f)

        # 書き込み後の読み直しが「キーが消えた状態」を返すよう細工する
        real_read = mcp_setup._read_json
        calls = {"n": 0}

        def flaky_read(path):
            calls["n"] += 1
            if calls["n"] == 1:
                return real_read(path)      # 書き込み前の読み込みは正常
            return {"mcpServers": {"scopus": {}}}   # 検証時に keepMe/other が消えている

        with patch.object(mcp_setup, "_read_json", side_effect=flaky_read):
            with pytest.raises(RuntimeError, match="dropped"):
                mcp_setup.register_claude_desktop(_entry(), path=cfg)

        # .bak から戻っていること
        assert json.loads(open(cfg).read()) == original

    def test_corrupt_json_is_not_overwritten(self, cfg):
        with open(cfg, "w") as f:
            f.write("{ this is not json")
        with pytest.raises(ValueError, match="not valid JSON"):
            mcp_setup.register_claude_desktop(_entry(), path=cfg)
        assert open(cfg).read() == "{ this is not json"

    def test_non_object_json_is_rejected(self, cfg):
        with open(cfg, "w") as f:
            f.write("[1, 2, 3]")
        with pytest.raises(ValueError):
            mcp_setup.register_claude_desktop(_entry(), path=cfg)

    def test_empty_file_is_treated_as_empty_config(self, cfg):
        open(cfg, "w").close()
        mcp_setup.register_claude_desktop(_entry(), path=cfg)
        assert "scopus" in json.loads(open(cfg).read())["mcpServers"]


class TestUnregister:
    def test_removes_only_our_entry(self, cfg):
        with open(cfg, "w") as f:
            json.dump({"mcpServers": {"other": {"command": "/bin/true"}}}, f)
        mcp_setup.register_claude_desktop(_entry(), path=cfg)
        res = mcp_setup.unregister_claude_desktop(path=cfg)
        assert res["removed"] is True
        data = json.loads(open(cfg).read())
        assert "scopus" not in data["mcpServers"]
        assert "other" in data["mcpServers"]

    def test_absent_registration_is_not_an_error(self, cfg):
        assert mcp_setup.unregister_claude_desktop(path=cfg)["removed"] is False


class TestClaudeCodeCommand:
    def test_command_shape(self):
        entry = mcp_setup.build_entry(with_keys=True, env={"SCOPUS_API_KEY": "abc"})
        cmd = mcp_setup.claude_code_command(entry, name="scopus", scope="user")
        assert cmd[:4] == ["claude", "mcp", "add", "scopus"]
        assert "--scope" in cmd and "user" in cmd
        assert "-e" in cmd and "SCOPUS_API_KEY=abc" in cmd
        # 実行対象は `--` の後ろ、絶対パス
        sep = cmd.index("--")
        assert os.path.isabs(cmd[sep + 1])

    def test_missing_claude_cli_is_a_clear_error(self):
        with patch("shutil.which", return_value=None):
            with pytest.raises(RuntimeError, match="claude"):
                mcp_setup.run_claude_code(_entry())

    def test_failure_surfaces_stderr(self):
        from unittest.mock import MagicMock
        proc = MagicMock(returncode=1, stdout="", stderr="boom")
        with patch("shutil.which", return_value="/usr/bin/claude"), \
             patch("subprocess.run", return_value=proc):
            with pytest.raises(RuntimeError, match="boom"):
                mcp_setup.run_claude_code(_entry())

    def test_success_returns_command(self):
        from unittest.mock import MagicMock
        proc = MagicMock(returncode=0, stdout="Added", stderr="")
        with patch("shutil.which", return_value="/usr/bin/claude"), \
             patch("subprocess.run", return_value=proc) as run:
            res = mcp_setup.run_claude_code(_entry())
        assert res["output"] == "Added"
        assert run.call_args.args[0][:3] == ["claude", "mcp", "add"]


class TestEnvPermissions:
    def test_detects_world_readable(self, tmp_path):
        env = tmp_path / ".env"
        env.write_text("SCOPUS_API_KEY=x\n")
        env.chmod(0o644)
        res = mcp_setup.check_env_permissions(str(env))
        assert res["world_readable"] is True
        assert res["fixed"] is False

    def test_fix_makes_it_owner_only(self, tmp_path):
        env = tmp_path / ".env"
        env.write_text("SCOPUS_API_KEY=x\n")
        env.chmod(0o644)
        res = mcp_setup.check_env_permissions(str(env), fix=True)
        assert res["fixed"] is True
        assert stat.S_IMODE(os.stat(env).st_mode) == 0o600

    def test_already_private_is_fine(self, tmp_path):
        env = tmp_path / ".env"
        env.write_text("SCOPUS_API_KEY=x\n")
        env.chmod(0o600)
        assert mcp_setup.check_env_permissions(str(env))["world_readable"] is False


class TestTccProtectedPaths:
    """~/Documents 配下を登録すると GUI クライアントからは起動すらできない。

    実測した失敗:
      PermissionError: [Errno 1] Operation not permitted: '.../.venv/pyvenv.cfg'
    ターミナル経由の Claude Code は権限があるため通ってしまい、気付きにくい。
    """

    @pytest.mark.parametrize("folder", ["Documents", "Desktop", "Downloads"])
    def test_detects_protected_folders(self, folder, monkeypatch):
        monkeypatch.setattr(mcp_setup.sys, "platform", "darwin")
        home = os.path.expanduser("~")
        assert mcp_setup.tcc_protected(f"{home}/{folder}/proj/.venv/bin/x") == folder

    def test_safe_paths_are_not_flagged(self, monkeypatch):
        monkeypatch.setattr(mcp_setup.sys, "platform", "darwin")
        home = os.path.expanduser("~")
        assert mcp_setup.tcc_protected(f"{home}/.local/bin/scopus-tools") is None
        assert mcp_setup.tcc_protected(f"{home}/.scopus-tools/index") is None
        assert mcp_setup.tcc_protected(None) is None

    def test_not_applied_off_macos(self, monkeypatch):
        monkeypatch.setattr(mcp_setup.sys, "platform", "linux")
        home = os.path.expanduser("~")
        assert mcp_setup.tcc_protected(f"{home}/Documents/x") is None

    def test_warns_about_the_executable(self, monkeypatch):
        monkeypatch.setattr(mcp_setup.sys, "platform", "darwin")
        home = os.path.expanduser("~")
        entry = {"command": f"{home}/Documents/repo/.venv/bin/scopus-tools", "args": ["mcp"]}
        problems = mcp_setup.tcc_warnings(entry)
        assert len(problems) == 1
        assert problems[0][1] == "Documents"

    def test_warns_about_data_directories(self, monkeypatch):
        monkeypatch.setattr(mcp_setup.sys, "platform", "darwin")
        home = os.path.expanduser("~")
        entry = {"command": f"{home}/.local/bin/scopus-tools",
                 "args": ["mcp", "--scie-dir", f"{home}/Documents/repo/index",
                          "--projects-dir", f"{home}/.scopus-tools/projects"]}
        problems = mcp_setup.tcc_warnings(entry)
        assert [p[2] for p in problems] == ["--scie-dir"]

    def test_clean_entry_has_no_warnings(self, monkeypatch):
        monkeypatch.setattr(mcp_setup.sys, "platform", "darwin")
        home = os.path.expanduser("~")
        entry = {"command": f"{home}/.local/bin/scopus-tools",
                 "args": ["mcp", "--scie-dir", f"{home}/.scopus-tools/index"]}
        assert mcp_setup.tcc_warnings(entry) == []
