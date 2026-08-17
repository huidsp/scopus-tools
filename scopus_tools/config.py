"""API キーを置く `.env` の読み書き。

置き場所は `~/.scopus-tools/.env` に統一している。`uv tool install` で入れた場合は
リポジトリのクローンが手元に無いので、どのディレクトリから実行しても読まれる場所が
1 つ必要になる。キャッシュ DB とプロジェクト JSON も同じディレクトリに住んでいる。

書き込みは必ず **600**(所有者のみ)で行う。API キーが平文で入るため。
"""

import logging
import os
import stat
import tempfile

from scopus_tools.cachedb import default_state_dir

logger = logging.getLogger(__name__)

# 設定できるキー。ここに無い名前は受け付けない(打ち間違いを黙って書かないため)。
KNOWN_KEYS = ("SCOPUS_API_KEY", "KAKEN_APP_ID", "WOS_API_KEY")


def user_env_path():
    """どこから実行しても読まれる `.env` のパス: `~/.scopus-tools/.env`"""
    return os.path.join(default_state_dir(), ".env")


def read_env_file(path=None):
    """`.env` を {キー: 値} で読む。無ければ空 dict。

    コメント行と空行は捨てる。`export FOO=bar` 形式も受ける。
    """
    path = path or user_env_path()
    if not os.path.exists(path):
        return {}
    values = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[len("export "):].lstrip()
            key, sep, value = line.partition("=")
            if not sep:
                continue
            values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def write_env_file(values, path=None):
    """`.env` をアトミックに、権限 600 で書く。

    他のツールが置いた未知のキーも保持する(このファイルを専有しない)。
    """
    path = path or user_env_path()
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=parent, prefix=".env-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            for key, value in values.items():
                f.write(f"{key}={value}\n")
        os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)   # 600
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise
    return path


def set_keys(assignments, path=None):
    """`{"SCOPUS_API_KEY": "..."}` を既存の内容に merge して保存する。

    未知のキー名は `ValueError`。値が空なら `ValueError`。
    """
    unknown = [k for k in assignments if k not in KNOWN_KEYS]
    if unknown:
        raise ValueError(
            f"unknown key(s): {', '.join(unknown)}. "
            f"Known keys are: {', '.join(KNOWN_KEYS)}")
    empty = [k for k, v in assignments.items() if not str(v).strip()]
    if empty:
        raise ValueError(f"empty value for: {', '.join(empty)}")

    values = read_env_file(path)
    values.update({k: str(v).strip() for k, v in assignments.items()})
    return write_env_file(values, path)


def unset_keys(keys, path=None):
    """指定したキーを削除する。戻り値は実際に消えたキー。"""
    values = read_env_file(path)
    removed = [k for k in keys if k in values]
    for k in removed:
        values.pop(k)
    if removed:
        write_env_file(values, path)
    return removed


def mask(value):
    """鍵を画面に出すための伏せ字。末尾 4 文字だけ残す。"""
    if not value:
        return ""
    s = str(value)
    if len(s) <= 4:
        return "*" * len(s)
    return "*" * (len(s) - 4) + s[-4:]


def parse_assignments(items):
    """`["KEY=VALUE", "KEY2"]` を ({KEY: VALUE}, [値の無いキー]) に分解する。

    値の無いキーは呼び出し側で入力を促す(コマンドラインに鍵を書かせないため)。
    """
    assignments = {}
    need_value = []
    for item in items or []:
        key, sep, value = str(item).partition("=")
        key = key.strip()
        if not key:
            raise ValueError(f"invalid argument: {item!r}")
        if sep and value.strip():
            assignments[key] = value.strip()
        else:
            need_value.append(key)
    return assignments, need_value


def describe(path=None):
    """現在の設定を表示用にまとめる(値は伏せる)。"""
    path = path or user_env_path()
    values = read_env_file(path)
    exists = os.path.exists(path)
    mode = oct(stat.S_IMODE(os.stat(path).st_mode)) if exists else None
    world_readable = bool(
        exists and os.stat(path).st_mode & (stat.S_IRWXG | stat.S_IRWXO))
    return {
        "path": path,
        "exists": exists,
        "mode": mode,
        "world_readable": world_readable,
        "keys": {k: mask(values.get(k)) for k in KNOWN_KEYS if values.get(k)},
        "missing": [k for k in KNOWN_KEYS if not values.get(k)],
        "other_keys": sorted(k for k in values if k not in KNOWN_KEYS),
    }
