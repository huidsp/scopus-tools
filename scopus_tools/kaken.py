import os
import logging
import xml.etree.ElementTree as ET
import requests

logger = logging.getLogger(__name__)


class KakenClient:
    """
    KAKEN (NII) OpenSearch API client.

    Requires KAKEN_APP_ID environment variable (CiNii API application ID).
    See: https://support.nii.ac.jp/ja/cinii/api/developer
    """

    PROJECT_ENDPOINT = "https://kaken.nii.ac.jp/opensearch/"
    RESEARCHER_ENDPOINT = "https://nrid.nii.ac.jp/opensearch/"

    def __init__(self, app_id=None):
        self.app_id = app_id or os.getenv("KAKEN_APP_ID")
        if not self.app_id:
            raise ValueError("KAKEN_APP_ID is not set.")
        logger.debug("KakenClient initialized.")

    def _request(self, url, params):
        params = {**params, "appid": self.app_id}
        safe_params = {k: v for k, v in params.items() if k != "appid"}
        logger.debug("Requesting %s with params %s", url, safe_params)
        resp = requests.get(url, params=params, headers={"User-Agent": "scopus-tools/kaken-client"})
        if resp.status_code != 200:
            snippet = resp.text[:300].replace("\n", " ")
            logger.warning("KAKEN request failed: status=%s body=%s", resp.status_code, snippet)
            return None
        return resp

    def search_researcher_by_name(self, name, lang="ja", rw=50):
        """Search researchers by name. Returns list of researcher dicts."""
        resp = self._request(
            self.RESEARCHER_ENDPOINT,
            {"qg": name, "format": "json", "lang": lang, "rw": rw},
        )
        if resp is None:
            return []
        try:
            data = resp.json()
        except ValueError:
            logger.warning("Researcher search returned non-JSON: %s", resp.text[:200])
            return []
        return _parse_researcher_json(data, lang=lang)

    def search_researcher_by_id(self, researcher_id, lang="ja"):
        """Look up a researcher by 8-digit KAKEN researcher number."""
        # rw must be one of {20, 50, 100, 200, 500} per KAKEN API doc;
        # smaller values silently return 0 results.
        resp = self._request(
            self.RESEARCHER_ENDPOINT,
            {"qm": str(researcher_id), "format": "json", "lang": lang, "rw": 20},
        )
        if resp is None:
            return None
        try:
            data = resp.json()
        except ValueError:
            return None
        results = _parse_researcher_json(data, lang=lang)
        return results[0] if results else None

    def get_grants_by_researcher_id(self, researcher_id, lang="ja", rw=500, role=None):
        """
        Fetch all research projects (grants) for a researcher number.

        role: optional list of role codes (e.g., ["principal_investigator"]).
              See KAKEN_API_parameters_document_Ja.pdf parameter c2.
        """
        params = {
            "qm": str(researcher_id),
            "format": "xml",
            "lang": lang,
            "rw": rw,
            "od": 3,  # 研究開始年:古い順
        }
        if role:
            params["c2"] = ",".join(role) if isinstance(role, (list, tuple)) else role
        resp = self._request(self.PROJECT_ENDPOINT, params)
        if resp is None:
            return []
        return _parse_project_xml(resp.content, researcher_id=str(researcher_id), lang=lang)


# ---------------------------------------------------------------------------
# JSON helpers (NRID researcher search)
# ---------------------------------------------------------------------------

def _pick_text(human_readable, lang="ja"):
    """Pick the best text from a humanReadableValue list given a language preference."""
    if not human_readable:
        return ""
    if isinstance(human_readable, dict):
        human_readable = [human_readable]
    # Prefer exact lang match, then fall back to any non-kana value, then first
    for item in human_readable:
        if item.get("lang") == lang:
            return item.get("text", "").strip()
    for item in human_readable:
        if item.get("lang") not in ("ja-Kana",):
            return item.get("text", "").strip()
    return human_readable[0].get("text", "").strip()


def _parse_researcher_json(data, lang="ja"):
    if not isinstance(data, dict):
        return []
    items = data.get("researchers") or []
    results = []
    for it in items:
        # researcher number
        erad = it.get("id:person:erad") or []
        researcher_id = erad[0] if isinstance(erad, list) and erad else ""

        # display name
        name_obj = it.get("name") or {}
        name = _pick_text(name_obj.get("humanReadableValue"), lang=lang)
        name_kana = _pick_text(name_obj.get("humanReadableValue"), lang="ja-Kana")

        # current affiliation
        cur_list = it.get("affiliations:current") or []
        cur = cur_list[0] if cur_list else {}
        inst = _pick_text((cur.get("affiliation:institution") or {}).get("humanReadableValue"), lang=lang)
        dept = _pick_text((cur.get("affiliation:department") or {}).get("humanReadableValue"), lang=lang)
        job = _pick_text((cur.get("affiliation:jobTitle") or {}).get("humanReadableValue"), lang=lang)

        results.append({
            "researcher_id": researcher_id,
            "name": name,
            "name_kana": name_kana,
            "affiliation": inst,
            "department": dept,
            "job_title": job,
            "project_count": len(it.get("work:project") or []),
            "product_count": len(it.get("work:product") or []),
            "raw": it,
        })
    return results


# ---------------------------------------------------------------------------
# XML helpers (KAKEN project search)
# ---------------------------------------------------------------------------

XML_LANG = "{http://www.w3.org/XML/1998/namespace}lang"


def _summary_for_lang(grant_award, lang="ja"):
    """Pick the <summary xml:lang="..."> child matching the requested language."""
    for s in grant_award.findall("summary"):
        if s.attrib.get(XML_LANG) == lang:
            return s
    summaries = grant_award.findall("summary")
    return summaries[0] if summaries else None


def _text(node, default=""):
    if node is None or node.text is None:
        return default
    return node.text.strip()


def _parse_project_xml(xml_bytes, researcher_id=None, lang="ja"):
    """
    Parse KAKEN grantAwards XML.

    Returns list of grant dicts.
    """
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as e:
        logger.warning("KAKEN XML parse error: %s", e)
        return []

    grants = []
    for ga in root.findall("grantAward"):
        award_number = ga.attrib.get("awardNumber", "")
        summary = _summary_for_lang(ga, lang=lang)
        if summary is None:
            continue

        title = _text(summary.find("title"))
        if not title:
            # Fall back to the other language's title (sometimes only English exists)
            for s in ga.findall("summary"):
                if s is summary:
                    continue
                fallback = _text(s.find("title"))
                if fallback:
                    title = fallback
                    break
        category = _text(summary.find("category"))             # 研究種目
        category_fc = _text(summary.find("categoryFc"))         # 細目
        review_section = _text(summary.find("review_section"))   # 審査区分
        section = _text(summary.find("section"))                 # 一般/その他
        allocation = _text(summary.find("allocation"))           # 補助金/基金
        institution = _text(summary.find("institution"))         # 研究機関

        period_node = summary.find("periodOfAward")
        period_from = period_node.attrib.get("searchStartFiscalYear", "") if period_node is not None else ""
        period_to = period_node.attrib.get("searchEndFiscalYear", "") if period_node is not None else ""

        status_node = summary.find("projectStatus")
        status = status_node.attrib.get("statusCode", "") if status_node is not None else ""
        status_year = status_node.attrib.get("fiscalYear", "") if status_node is not None else ""

        keywords = [_text(k) for k in summary.findall("keywordList/keyword")]

        # cost breakdown — overallAwardAmount with caption='配分額' (ja) or 'Budget Amount' (en)
        direct_cost = indirect_cost = total_cost = ""
        amount = summary.find("overallAwardAmount")
        if amount is not None:
            direct_cost = _text(amount.find("directCost"))
            indirect_cost = _text(amount.find("indirectCost"))
            total_cost = _text(amount.find("totalCost"))

        # members
        members = []
        my_role = None
        for m in summary.findall("member"):
            erad_code = m.attrib.get("eradCode", "")
            role_code = m.attrib.get("role", "")
            full_name = _text(m.find("personalName/fullName"))
            members.append({
                "researcher_id": erad_code,
                "name": full_name,
                "role": role_code,
                "institution": _text(m.find("institution")),
                "department": _text(m.find("department")),
                "job_title": _text(m.find("jobTitle")),
            })
            if researcher_id and erad_code == str(researcher_id):
                my_role = role_code

        grants.append({
            "project_number": award_number,
            "title": title,
            "grant_category": category,
            "category_detail": category_fc,
            "review_section": review_section,
            "section": section,
            "allocation": allocation,
            "institution": institution,
            "period_from": period_from,
            "period_to": period_to,
            "status": status,
            "status_year": status_year,
            "keywords": keywords,
            "direct_cost": direct_cost,
            "indirect_cost": indirect_cost,
            "total_cost": total_cost,
            "members": members,
            "my_role": my_role,
        })
    return grants
