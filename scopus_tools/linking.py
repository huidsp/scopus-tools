"""Scopus 著者プロフィール → KAKEN 研究者番号 の名前ベース自動マッチング。"""

import logging
import sys

logger = logging.getLogger(__name__)


def resolve_kaken_researcher_ids(
    scopus_first,
    scopus_last,
    kaken_client,
    lang="ja",
    auto=False,
    interactive=None,
):
    """Scopus 著者氏名から KAKEN 研究者番号を解決する。

    戻り値: 採用された researcher_id の文字列リスト(0 件もあり)。

    動作仕様:
      - 候補 0 件: 警告のみ出して空リスト。
      - 候補 1 件: 自動採用し採用内容を stderr に表示。
      - 候補 2 件以上 & auto=True: 先頭候補を採用。
      - 候補 2 件以上 & interactive=True: 番号入力で対話選択。
      - 候補 2 件以上 & 非対話: 候補を表示して空リストを返す
        (ユーザに --kaken-id か --kaken-auto を促す)。

    interactive=None の場合は stdin が TTY かどうかで自動判定する。
    """
    full_name = f"{scopus_first or ''} {scopus_last or ''}".strip()
    if not full_name:
        return []

    if interactive is None:
        interactive = sys.stdin.isatty()

    candidates = kaken_client.search_researcher_by_name(full_name, lang=lang)
    if not candidates:
        print(
            f"[KAKEN] No researcher matched '{full_name}'. "
            f"Continuing without grants.",
            file=sys.stderr,
        )
        return []

    if len(candidates) == 1:
        r = candidates[0]
        _announce_pick(r, prefix="Linked")
        return [r["researcher_id"]]

    if auto:
        r = candidates[0]
        _announce_pick(r, prefix=f"Auto-picked from {len(candidates)} matches")
        return [r["researcher_id"]]

    if not interactive:
        print(
            f"[KAKEN] Found {len(candidates)} candidates for '{full_name}'. "
            f"Specify --kaken-id explicitly or pass --kaken-auto.",
            file=sys.stderr,
        )
        for r in candidates[:5]:
            print(
                f"  - {r['researcher_id']}  {r['name']}  {r['affiliation']}",
                file=sys.stderr,
            )
        return []

    print(
        f"[KAKEN] {len(candidates)} researchers matched '{full_name}'. "
        f"Select one (0 to skip):",
        file=sys.stderr,
    )
    for i, r in enumerate(candidates, start=1):
        print(
            f"  {i}. {r['researcher_id']}  {r['name']}  {r['affiliation']}",
            file=sys.stderr,
        )
    while True:
        try:
            choice = input("Select: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("[KAKEN] Skipped.", file=sys.stderr)
            return []
        if choice in ("", "0"):
            return []
        if choice.isdigit() and 1 <= int(choice) <= len(candidates):
            return [candidates[int(choice) - 1]["researcher_id"]]
        print("Invalid choice; enter a number from the list.", file=sys.stderr)


def _announce_pick(researcher, prefix):
    print(
        f"[KAKEN] {prefix}: #{researcher['researcher_id']} "
        f"({researcher.get('name', '')}, {researcher.get('affiliation', '')})",
        file=sys.stderr,
    )
