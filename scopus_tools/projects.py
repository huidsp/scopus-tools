"""プロジェクト(複数研究者をまとめる単位)を JSON ファイルに永続化する CRUD レイヤ。

データ階層:
  Project (例: "Hiroshima Univ CS 2026")
    └── Researcher[] (例: Hiroyuki Okamura, Taro Tanaka, ...)
          └── scopus, kaken, ai セクション

旧フォーマット(プロジェクト直下に scopus/kaken/ai を持つ単一研究者形式)を読んだ場合は、
プロジェクト名と同じ名前の Researcher として自動マイグレートする。

Gradio / 他の UI フレームワーク非依存。webui.py から呼ぶ。
"""

import datetime
import json
import logging
import os
import re
import tempfile

logger = logging.getLogger(__name__)

_SAFE_NAME_RE = re.compile(r"[^-\w. ()ぁ-んァ-ヴ一-龯]")


def default_projects_dir():
    """既定の保存ディレクトリ: ~/.scopus-tools/projects/

    キャッシュ DB (`cachedb.default_db_path`) と同じ状態ディレクトリを共有する。
    """
    from scopus_tools.cachedb import default_state_dir

    return os.path.join(default_state_dir(), "projects")


def _sanitize_filename(name):
    base = _SAFE_NAME_RE.sub("_", name).strip(" .")
    return base or "untitled"


def _now_iso():
    return datetime.datetime.now().isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Researcher & Project shape helpers
# ---------------------------------------------------------------------------

def empty_researcher(name):
    """空の Researcher(研究者)エントリを返す。"""
    ts = _now_iso()
    return {
        "name": name,
        "created_at": ts,
        "updated_at": ts,
        "scopus": None,
        "kaken": None,
        "ai": None,
    }


def empty_project(name):
    """空のプロジェクト(researchers リストを持つ)を返す。"""
    ts = _now_iso()
    return {
        "name": name,
        "created_at": ts,
        "updated_at": ts,
        "researchers": [],
    }


def _migrate_if_legacy(data):
    """旧フォーマット(scopus/kaken/ai がトップレベル)を新フォーマットに変換する。

    旧: { "name": "X", "scopus": ..., "kaken": ..., "ai": ... }
    新: { "name": "X", "researchers": [ {"name": "X", "scopus": ..., ...} ] }
    """
    if not isinstance(data, dict):
        return data
    if "researchers" in data and isinstance(data["researchers"], list):
        return data
    # 旧フォーマット: トップレベルから scopus/kaken/ai を 1 研究者にまとめる
    name = data.get("name") or "Unknown"
    researcher = empty_researcher(name)
    for key in ("scopus", "kaken", "ai"):
        if data.get(key) is not None:
            researcher[key] = data[key]
    if researcher["scopus"] or researcher["kaken"] or researcher["ai"]:
        # 旧データの created_at/updated_at を研究者側に引き継ぐ
        if data.get("created_at"):
            researcher["created_at"] = data["created_at"]
        if data.get("updated_at"):
            researcher["updated_at"] = data["updated_at"]
        researchers = [researcher]
    else:
        researchers = []
    new = {
        "name": name,
        "created_at": data.get("created_at") or _now_iso(),
        "updated_at": data.get("updated_at") or _now_iso(),
        "researchers": researchers,
    }
    logger.info("Migrated legacy project '%s' to new format with %d researcher(s)",
                name, len(researchers))
    return new


def completion_flags(researcher):
    """Researcher の Scopus / KAKEN / AI それぞれの取得済みフラグ。"""
    if not isinstance(researcher, dict):
        return (False, False, False)
    s = researcher.get("scopus") or {}
    k = researcher.get("kaken") or {}
    a = researcher.get("ai") or {}
    return (
        bool(s.get("papers")),
        bool(k.get("grants")),
        bool(a.get("evaluation")),
    )


def find_researcher(project, name):
    """プロジェクト内の研究者を名前で検索。無ければ None。"""
    if not project or not name:
        return None
    for r in project.get("researchers") or []:
        if r.get("name") == name:
            return r
    return None


def add_researcher(project, name):
    """プロジェクトに研究者を追加して新エントリを返す。同名は呼び出し側で回避。"""
    researcher = empty_researcher(name)
    project.setdefault("researchers", []).append(researcher)
    return researcher


def remove_researcher(project, name):
    """プロジェクトから指定名の研究者を削除。"""
    researchers = project.get("researchers") or []
    project["researchers"] = [r for r in researchers if r.get("name") != name]
    return len(project["researchers"]) != len(researchers)


def rename_researcher(project, old_name, new_name):
    """プロジェクト内の研究者をリネーム。new_name が既存なら False を返す。"""
    if old_name == new_name:
        return True
    if find_researcher(project, new_name) is not None:
        return False
    r = find_researcher(project, old_name)
    if r is None:
        return False
    r["name"] = new_name
    r["updated_at"] = _now_iso()
    return True


def make_unique_researcher_name(project, base):
    """プロジェクト内で同名と衝突しないように suffix を付ける。"""
    if find_researcher(project, base) is None:
        return base
    n = 2
    while True:
        candidate = f"{base} ({n})"
        if find_researcher(project, candidate) is None:
            return candidate
        n += 1


def merge_researcher_section(project, researcher_name, section, partial):
    """指定研究者の指定セクション(scopus/kaken/ai)に partial を merge する。

    研究者が存在しなければ作成。updated_at を自動更新。
    """
    r = find_researcher(project, researcher_name)
    if r is None:
        r = add_researcher(project, researcher_name)
    r[section] = {**(r.get(section) or {}), **partial}
    r["updated_at"] = _now_iso()
    return r


def set_project_comparison(project, comparison):
    """プロジェクト直下の `comparison` キーを設定する(人事選考の比較結果保存用)。

    comparison は dict: {selected_names, lang, table_md, ai_evaluation, updated_at?}
    """
    data = dict(comparison or {})
    data.setdefault("updated_at", _now_iso())
    project["comparison"] = data
    return data


# ---------------------------------------------------------------------------
# ProjectStore
# ---------------------------------------------------------------------------

class ProjectStore:
    """指定ディレクトリ配下に 1 プロジェクト = 1 JSON ファイルで永続化する。"""

    def __init__(self, dir_path=None):
        self.dir_path = dir_path or default_projects_dir()
        os.makedirs(self.dir_path, exist_ok=True)

    # ---- パス解決 -------------------------------------------------

    def _path_for(self, name):
        return os.path.join(self.dir_path, _sanitize_filename(name) + ".json")

    def exists(self, name):
        return os.path.exists(self._path_for(name))

    # ---- 一覧 -----------------------------------------------------

    def list(self):
        """プロジェクトのメタ一覧を更新日時降順で返す。

        各要素は dict: { name, path, updated_at, researcher_count, completion_summary }
        """
        results = []
        if not os.path.isdir(self.dir_path):
            return results
        for fn in os.listdir(self.dir_path):
            if not fn.endswith(".json"):
                continue
            path = os.path.join(self.dir_path, fn)
            try:
                with open(path, encoding="utf-8") as f:
                    data = json.load(f)
            except (OSError, json.JSONDecodeError) as e:
                logger.warning("Skip unreadable project file %s: %s", path, e)
                continue
            data = _migrate_if_legacy(data)
            researchers = data.get("researchers") or []
            # 完了サマリ: それぞれのフラグを集計
            done_s = sum(1 for r in researchers if completion_flags(r)[0])
            done_k = sum(1 for r in researchers if completion_flags(r)[1])
            done_a = sum(1 for r in researchers if completion_flags(r)[2])
            results.append({
                "name": data.get("name") or os.path.splitext(fn)[0],
                "path": path,
                "updated_at": data.get("updated_at") or "",
                "researcher_count": len(researchers),
                "completion": (done_s, done_k, done_a),
            })
        results.sort(key=lambda r: r["updated_at"], reverse=True)
        return results

    # ---- CRUD -----------------------------------------------------

    def load(self, name):
        path = self._path_for(name)
        if not os.path.exists(path):
            return None
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            logger.warning("Failed to load project %s: %s", name, e)
            return None
        return _migrate_if_legacy(data)

    def save(self, name, data):
        """プロジェクトを保存(updated_at は自動更新)。アトミック書き込み。"""
        if not isinstance(data, dict):
            raise TypeError("project data must be a dict")
        data = dict(data)
        data["name"] = name
        data.setdefault("created_at", _now_iso())
        data["updated_at"] = _now_iso()
        data.setdefault("researchers", [])

        path = self._path_for(name)
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(
            prefix=".tmp_", suffix=".json", dir=self.dir_path,
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2, default=str)
            os.replace(tmp_path, path)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
        return path

    def delete(self, name):
        path = self._path_for(name)
        if os.path.exists(path):
            os.unlink(path)
            return True
        return False

    def rename(self, old_name, new_name):
        if not self.exists(old_name):
            return False
        if old_name == new_name:
            return True
        new_path = self._path_for(new_name)
        if os.path.exists(new_path):
            raise FileExistsError(f"project '{new_name}' already exists")
        data = self.load(old_name) or {}
        data["name"] = new_name
        self.save(new_name, data)
        self.delete(old_name)
        return True

    def make_unique_name(self, base):
        if not self.exists(base):
            return base
        n = 2
        while True:
            candidate = f"{base} ({n})"
            if not self.exists(candidate):
                return candidate
            n += 1
