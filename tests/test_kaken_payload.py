"""NRID 研究者検索の応答サイズに関する回帰テスト。

以前は API の item をそのまま `raw` として返しており、1 件が業績全件
(`work:product`)を含むため MCP の応答が実測 25 MB を超え、ツール呼び出しが
クライアント側で 60 秒タイムアウトしていた。同定に必要な項目だけを返すこと、
および名前検索の既定件数が最小であることを固定する。
"""
import json

from scopus_tools.kaken import _parse_researcher_json, KakenClient


def _item(name="山田 花子", erad="10000001", products=0):
    """NRID の 1 研究者分の応答を模す。products で肥大部分の量を変える。"""
    return {
        "id:person:erad": [erad],
        "name": {"humanReadableValue": [
            {"lang": "ja", "text": name},
            {"lang": "ja-Kana", "text": "ヤマダ ハナコ"},
        ]},
        "affiliations:current": [{
            "affiliation:institution": {
                "humanReadableValue": [{"lang": "ja", "text": "○○大学"}]},
            "affiliation:department": {
                "humanReadableValue": [{"lang": "ja", "text": "工学部"}]},
            "affiliation:jobTitle": {
                "humanReadableValue": [{"lang": "ja", "text": "教授"}]},
        }],
        "work:project": [{"id": f"p{i}"} for i in range(3)],
        # 実物ではここが 1 件あたり数百 KB になる
        "work:product": [{"title": "x" * 200} for i in range(products)],
    }


class TestResearcherPayload:

    def test_useful_fields_are_kept(self):
        (r,) = _parse_researcher_json({"researchers": [_item()]})
        assert r["researcher_id"] == "10000001"
        assert r["name"] == "山田 花子"
        assert r["name_kana"] == "ヤマダ ハナコ"
        assert r["affiliation"] == "○○大学"
        assert r["department"] == "工学部"
        assert r["job_title"] == "教授"
        # 件数は数字だけ残す(中身は落とす)
        assert r["project_count"] == 3
        assert r["product_count"] == 0

    def test_raw_api_item_is_not_returned(self):
        """`raw` を返さないこと。返すと MCP の応答が数十 MB になる。"""
        (r,) = _parse_researcher_json({"researchers": [_item(products=500)]})
        assert "raw" not in r
        assert set(r) == {
            "researcher_id", "name", "name_kana", "affiliation",
            "department", "job_title", "project_count", "product_count",
        }

    def test_payload_stays_small_for_prolific_researchers(self):
        """多作な研究者 20 件でも応答が小さいままであること。"""
        heavy = {"researchers": [
            _item(erad=f"1000000{i}", products=500) for i in range(20)]}
        # 入力自体は肥大している
        assert len(json.dumps(heavy)) > 2_000_000
        out = json.dumps(_parse_researcher_json(heavy), ensure_ascii=False)
        assert len(out) < 10_000, f"researcher search payload too large: {len(out)}"

    def test_product_count_survives_without_the_list(self):
        (r,) = _parse_researcher_json({"researchers": [_item(products=137)]})
        assert r["product_count"] == 137


class TestSearchRows:

    def test_name_search_requests_the_smallest_page(self, monkeypatch):
        """rw は NRID が許す最小値(20)を既定にする。大きいほど応答が重い。"""
        monkeypatch.setenv("KAKEN_APP_ID", "dummy-app-id")
        seen = {}

        class _Http:
            def get(self, url, params=None, headers=None, api=None):
                seen.update(params or {})
                raise AssertionError("stop after capturing params")

        client = KakenClient(http=_Http())
        try:
            client.search_researcher_by_name("山田 花子")
        except AssertionError:
            pass
        assert seen["rw"] == 20
