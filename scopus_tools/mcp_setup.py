"""MCP クライアントへの登録を自動化する。

MCP の「インストール」で実際に人が詰まるのは次の 2 点で、どちらもここで潰す:

1. **絶対パスが要る。** MCP クライアントはシェルを経由せずにプロセスを起動するので、
   venv の有効化も `PATH` も引き継がれない。`scopus-tools` とだけ書いても見つからない。
2. **シェルの `export` が届かない。** 同じ理由で API キーは登録時に環境変数として
   渡さないと、ツール呼び出しが必ず「キーが無い」で失敗する。

設定ファイルを書き換えるときは、必ず `.bak` を取り、**書いた後に読み直して
元のキーが 1 つも失われていないことを検証**する。失われていたら書き戻して中止する。
"""

import json
import logging
import os
import shutil
import stat
import subprocess
import sys
import tempfile

logger = logging.getLogger(__name__)

DEFAULT_SERVER_NAME = "scopus"

# 登録時に引き継ぐ環境変数。ここに無いものは埋め込まない。
KEY_NAMES = ("SCOPUS_API_KEY", "KAKEN_APP_ID")


# macOS の TCC(プライバシー保護)が守るディレクトリ。GUI アプリから起動された
# プロセスは既定でここを読めない。
#
# **実測した失敗**: `~/Documents/.../.venv/bin/scopus-tools` を Claude Desktop に
# 登録すると、Python が `pyvenv.cfg` すら読めずに落ちる:
#   Fatal Python error: init_import_site: Failed to import the site module
#   PermissionError: [Errno 1] Operation not permitted: '.../.venv/pyvenv.cfg'
# ターミナル経由の Claude Code では権限があるため通ってしまい、気付きにくい。
TCC_PROTECTED_DIRS = ("Documents", "Desktop", "Downloads")


def tcc_protected(path):
    """パスが macOS の TCC 保護ディレクトリ配下かどうか。

    保護対象でなければ None、対象ならその親ディレクトリ名を返す。
    """
    if sys.platform != "darwin" or not path:
        return None
    home = os.path.realpath(os.path.expanduser("~"))
    real = os.path.realpath(os.path.expanduser(str(path)))
    for name in TCC_PROTECTED_DIRS:
        root = os.path.join(home, name)
        if real == root or real.startswith(root + os.sep):
            return name
    return None


def tcc_warnings(entry):
    """登録内容のうち TCC 保護配下にあるパスを洗い出す。

    ここに引っかかったまま登録すると、Claude Desktop からは起動すらできない。
    """
    problems = []
    protected = tcc_protected(entry.get("command"))
    if protected:
        problems.append((entry["command"], protected, "the executable"))
    args = entry.get("args") or []
    for i, arg in enumerate(args):
        if arg in ("--scie-dir", "--projects-dir", "--cache-db", "--scie-list"):
            if i + 1 < len(args):
                value = args[i + 1]
                protected = tcc_protected(value)
                if protected:
                    problems.append((value, protected, arg))
    return problems


def claude_desktop_config_path():
    """Claude Desktop の設定ファイルのパス(OS ごと)。"""
    if sys.platform == "darwin":
        return os.path.expanduser(
            "~/Library/Application Support/Claude/claude_desktop_config.json")
    if os.name == "nt":                                  # pragma: no cover
        base = os.environ.get("APPDATA", os.path.expanduser("~"))
        return os.path.join(base, "Claude", "claude_desktop_config.json")
    return os.path.expanduser(                            # pragma: no cover
        "~/.config/Claude/claude_desktop_config.json")


def resolve_command():
    """この CLI 自身を起動する **絶対パス** を解決する。

    1. `sys.argv[0]` が実行可能ファイルなら、その実体(`uv tool install` は
       symlink を張るので `realpath` で実体まで解決する)
    2. `PATH` 上の `scopus-tools`
    3. どちらも駄目なら `python -m scopus_tools.cli`

    戻り値は `(command, args_prefix)`。
    """
    argv0 = sys.argv[0] if sys.argv else ""
    if argv0:
        candidate = os.path.realpath(argv0)
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate, []

    found = shutil.which("scopus-tools")
    if found:
        return os.path.realpath(found), []

    # 最後の手段。python 自体は必ず絶対パスで取れる。
    return os.path.realpath(sys.executable), ["-m", "scopus_tools.cli"]


def collect_keys(env=None):
    """登録に埋め込む API キーを集める。見つかったものだけを返す。"""
    env = os.environ if env is None else env
    return {name: env[name] for name in KEY_NAMES if env.get(name)}


def build_server_args(scie_dir=None, scie_list=None, projects_dir=None, cache_db=None):
    """`mcp` サブコマンドに渡す引数列を組み立てる(パスはすべて絶対化)。"""
    args = ["mcp"]
    if projects_dir:
        args += ["--projects-dir", os.path.abspath(projects_dir)]
    if scie_dir:
        args += ["--scie-dir", os.path.abspath(scie_dir)]
    for path in (scie_list or []):
        args += ["--scie-list", os.path.abspath(path)]
    if cache_db:
        args += ["--cache-db", os.path.abspath(cache_db)]
    return args


def build_entry(scie_dir=None, scie_list=None, projects_dir=None, cache_db=None,
                with_keys=True, env=None):
    """MCP クライアントに書き込む 1 エントリ分の dict を作る。"""
    command, prefix = resolve_command()
    entry = {"command": command, "args": prefix + build_server_args(
        scie_dir=scie_dir, scie_list=scie_list,
        projects_dir=projects_dir, cache_db=cache_db)}
    if with_keys:
        keys = collect_keys(env)
        if keys:
            entry["env"] = keys
    return entry


# ---------------------------------------------------------------------------
# 設定ファイルの安全な読み書き
# ---------------------------------------------------------------------------

def _read_json(path):
    """設定 JSON を読む。無ければ空 dict。壊れていれば ValueError。"""
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        text = f.read().strip()
    if not text:
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise ValueError(
            f"{path} is not valid JSON ({e}). Refusing to overwrite it — "
            f"fix or move the file first.") from e
    if not isinstance(data, dict):
        raise ValueError(f"{path} does not contain a JSON object; refusing to overwrite it.")
    return data


def _atomic_write_json(path, data):
    """アトミックに書く(ProjectStore と同じ tempfile + os.replace)。

    設定ファイルには API キーが入りうるので、権限は 600 で作る。
    """
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=parent, prefix=".scopus-tools-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


def _key_inventory(data):
    """検証用に「失ってはいけないもの」を洗い出す。"""
    return {
        "top_level": set(data.keys()),
        "servers": set((data.get("mcpServers") or {}).keys()),
    }


def register_claude_desktop(entry, name=DEFAULT_SERVER_NAME, path=None):
    """Claude Desktop の設定に 1 エントリ追記・更新する。

    書く前に `.bak` を取り、書いた後に読み直して**元のトップレベルキーと
    他サーバのエントリが全部残っていること**を検証する。失われていたら書き戻す。
    """
    path = path or claude_desktop_config_path()
    data = _read_json(path)
    before = _key_inventory(data)

    backup = None
    if os.path.exists(path):
        backup = path + ".bak"
        shutil.copy2(path, backup)

    servers = dict(data.get("mcpServers") or {})
    replaced = name in servers
    servers[name] = entry
    data["mcpServers"] = servers
    _atomic_write_json(path, data)

    # 検証: 何も失っていないか
    written = _read_json(path)
    after = _key_inventory(written)
    lost_top = before["top_level"] - after["top_level"]
    lost_servers = before["servers"] - after["servers"]
    if lost_top or lost_servers:
        if backup:
            shutil.copy2(backup, path)
        raise RuntimeError(
            f"Writing {path} would have dropped "
            f"{sorted(lost_top | lost_servers)}; restored from {backup} and aborted.")

    return {"path": path, "backup": backup, "replaced": replaced, "name": name}


def unregister_claude_desktop(name=DEFAULT_SERVER_NAME, path=None):
    path = path or claude_desktop_config_path()
    if not os.path.exists(path):
        return {"path": path, "removed": False}
    data = _read_json(path)
    servers = dict(data.get("mcpServers") or {})
    if name not in servers:
        return {"path": path, "removed": False}
    backup = path + ".bak"
    shutil.copy2(path, backup)
    servers.pop(name)
    data["mcpServers"] = servers
    _atomic_write_json(path, data)
    return {"path": path, "removed": True, "backup": backup}


def desktop_status(name=DEFAULT_SERVER_NAME, path=None):
    path = path or claude_desktop_config_path()
    try:
        data = _read_json(path)
    except ValueError as e:
        return {"path": path, "registered": False, "error": str(e)}
    entry = (data.get("mcpServers") or {}).get(name)
    return {"path": path, "registered": entry is not None, "entry": entry}


# ---------------------------------------------------------------------------
# Claude Code (`claude mcp add`)
# ---------------------------------------------------------------------------

def claude_code_command(entry, name=DEFAULT_SERVER_NAME, scope=None):
    """`claude mcp add ...` のコマンド列を組み立てる。

    設定ファイルを直接いじらず公式 CLI に任せる(スコープの扱いを任せられる)。
    """
    cmd = ["claude", "mcp", "add", name]
    if scope:
        cmd += ["--scope", scope]
    for key, value in (entry.get("env") or {}).items():
        cmd += ["-e", f"{key}={value}"]
    cmd += ["--", entry["command"], *entry["args"]]
    return cmd


def run_claude_code(entry, name=DEFAULT_SERVER_NAME, scope=None):
    if shutil.which("claude") is None:
        raise RuntimeError(
            "The `claude` CLI was not found on PATH. Install Claude Code, or use "
            "--print and run the command yourself.")
    cmd = claude_code_command(entry, name=name, scope=scope)
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(
            f"`claude mcp add` failed (exit {proc.returncode}): "
            f"{(proc.stderr or proc.stdout).strip()}")
    return {"command": cmd, "output": (proc.stdout or "").strip()}


def remove_claude_code(name=DEFAULT_SERVER_NAME, scope=None):
    if shutil.which("claude") is None:
        raise RuntimeError("The `claude` CLI was not found on PATH.")
    cmd = ["claude", "mcp", "remove", name]
    if scope:
        cmd += ["--scope", scope]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    return {"command": cmd, "ok": proc.returncode == 0,
            "output": (proc.stdout or proc.stderr or "").strip()}


# ---------------------------------------------------------------------------
# .env の権限チェック
# ---------------------------------------------------------------------------

def user_env_path():
    """どこから実行しても読まれる `.env`: `~/.scopus-tools/.env`"""
    from scopus_tools.cachedb import default_state_dir

    return os.path.join(default_state_dir(), ".env")


def find_env_file():
    """`.env` の場所を探す(カレント基準 → ~/.scopus-tools → パッケージ基準)。"""
    from dotenv import find_dotenv

    for path in (find_dotenv(usecwd=True), user_env_path(), find_dotenv()):
        if path and os.path.exists(path):
            return path
    return None


def check_env_permissions(path=None, fix=False):
    """`.env` が他人から読める権限になっていないか調べる。

    fix=True なら 600 に直す。勝手には変更しない。
    """
    path = path or find_env_file()
    if not path:
        return None
    mode = stat.S_IMODE(os.stat(path).st_mode)
    group_other = mode & (stat.S_IRWXG | stat.S_IRWXO)
    result = {"path": path, "mode": oct(mode), "world_readable": bool(group_other),
              "fixed": False}
    if group_other and fix:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
        result["fixed"] = True
        result["mode"] = oct(stat.S_IMODE(os.stat(path).st_mode))
    return result
