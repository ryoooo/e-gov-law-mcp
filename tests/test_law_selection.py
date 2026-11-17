#!/usr/bin/env python3
"""
法令の選択の回帰テスト

- 題名検索の候補から、題名が完全一致する法令を選ぶこと
  （消費税法が「…消費税法の一部を改正する等の法律」に化けない）
"""

import base64
import json
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from fastmcp import Client

import mcp_server
from mcp_server import (
    cache_manager,
    get_law_title,
    law_title_matches,
    mcp,
    select_best_law,
)


def law(law_num, title, promulgation_date="", abbrev=None, repeal_status="None"):
    """e-Gov API v2 の /laws の 1 件分（必要な項目のみ）"""
    return {
        "law_info": {"law_num": law_num, "promulgation_date": promulgation_date},
        "revision_info": {"law_title": title, "abbrev": abbrev, "repeal_status": repeal_status},
    }


# /laws?law_title=消費税法&law_type=Act の実際の結果（2026-11 時点）
SHOHIZEI_CANDIDATES = [
    law("昭和六十三年法律第百八号", "消費税法", "1988-12-30"),
    law(
        "平成六年法律第百八号",
        "所得税法及び消費税法の一部を改正する法律の施行等による租税収入の減少を補うための平成六年度から平成八年度までの公債の発行の特例等に関する法律",
        "1994-11-25",
    ),
    law(
        "平成二十四年法律第六十八号",
        "社会保障の安定財源の確保等を図る税制の抜本的な改革を行うための消費税法の一部を改正する等の法律",
        "2012-08-22",
    ),
]

# /laws?law_title=地方税法&law_type=Act
CHIHOUZEI_CANDIDATES = [
    law(
        "昭和四十四年法律第四十六号",
        "租税条約等の実施に伴う所得税法、法人税法及び地方税法の特例等に関する法律",
        "1969-05-30",
    ),
    law("昭和二十五年法律第二百二十六号", "地方税法", "1950-07-31"),
    law(
        "昭和二十九年法律第百八十八号",
        "日本国における国際連合の軍隊の地位に関する協定の実施に伴う地方税法の臨時特例に関する法律",
        "1954-06-19",
    ),
]

# /laws?law_title=所得税法&law_type=Act（令和の特例法が最も新しい）
SHOTOKUZEI_CANDIDATES = [
    law("昭和四十年法律第三十三号", "所得税法", "1965-03-31"),
    law(
        "令和六年法律第一号",
        "令和六年能登半島地震災害の被災者に係る所得税法及び災害被害者に対する租税の減免、徴収猶予等に関する法律の臨時特例に関する法律",
        "2024-01-26",
    ),
]


class TestSelectBestLaw:
    """題名検索の候補から正しい法令を選ぶ"""

    @pytest.mark.parametrize(
        "name, candidates, expected",
        [
            ("消費税法", SHOHIZEI_CANDIDATES, "昭和六十三年法律第百八号"),
            ("地方税法", CHIHOUZEI_CANDIDATES, "昭和二十五年法律第二百二十六号"),
            ("所得税法", SHOTOKUZEI_CANDIDATES, "昭和四十年法律第三十三号"),
        ],
    )
    def test_exact_title_wins_over_newer_laws(self, name, candidates, expected):
        best = select_best_law(candidates, name)
        assert best["law_info"]["law_num"] == expected
        assert law_title_matches(best, name)

    def test_candidate_order_does_not_matter(self):
        best = select_best_law(list(reversed(SHOHIZEI_CANDIDATES)), "消費税法")
        assert best["law_info"]["law_num"] == "昭和六十三年法律第百八号"

    def test_law_number_prefixed_title_and_abbrev(self):
        """独占禁止法は題名が「昭和二十二年法律第五十四号（…）」の形で、略称が登録されている"""
        dokkin = law(
            "昭和二十二年法律第五十四号",
            "昭和二十二年法律第五十四号（私的独占の禁止及び公正取引の確保に関する法律）",
            abbrev="独禁法,独占禁止法",
        )
        other = law(
            "令和元年法律第四十五号",
            "私的独占の禁止及び公正取引の確保に関する法律の一部を改正する法律",
        )
        assert select_best_law([other, dokkin], "独占禁止法") is dokkin
        assert (
            select_best_law([other, dokkin], "私的独占の禁止及び公正取引の確保に関する法律")
            is dokkin
        )
        assert law_title_matches(dokkin, "独禁法")

    def test_in_force_law_preferred_over_repealed(self):
        old = law("昭和二十五年法律第一号", "テスト法", repeal_status="Repeal")
        new = law("令和二年法律第一号", "テスト法")
        assert select_best_law([old, new], "テスト法") is new

    def test_without_exact_match_prefers_shortest_title(self):
        best = select_best_law(SHOHIZEI_CANDIDATES[1:], "消費税")
        assert best["law_info"]["law_num"] == "平成二十四年法律第六十八号"
        assert not law_title_matches(best, "消費税")

    def test_empty(self):
        assert select_best_law([], "消費税法") is None

    def test_get_law_title_reads_revision_info(self):
        """API v2 では law_info に題名がない"""
        assert get_law_title(SHOHIZEI_CANDIDATES[0]) == "消費税法"
        assert get_law_title({"law_info": {"law_num": "x"}}) == ""
        assert get_law_title({"law_info": {"law_title": "X"}}) == ""


# /law_data の応答のうち、条文は取れるが題名が空のもの（ネットワークなし）
TITLELESS_LAW_XML = (
    "<Law><LawBody><MainProvision>"
    '<Article Num="1"><ArticleTitle>第一条</ArticleTitle>'
    '<Paragraph Num="1"><ParagraphNum/><ParagraphSentence><Sentence>テスト。</Sentence>'
    "</ParagraphSentence></Paragraph></Article></MainProvision></LawBody></Law>"
)


@pytest.fixture
def titleless_law_data(monkeypatch):
    """題名が空の /law_data 応答を返す。法令番号の検索とキャッシュの先読みは差し替える"""
    law_data = {
        "law_info": {"law_num": "昭和六十三年法律第百八号", "promulgation_date": "1988-12-30"},
        "revision_info": {"law_title": "", "abbrev": None, "repeal_status": "None"},
        "law_full_text": base64.b64encode(TITLELESS_LAW_XML.encode("utf-8")).decode("ascii"),
    }

    async def fake_smart_law_lookup(law_name):
        return "昭和六十三年法律第百八号"

    async def fake_http_client():
        return httpx.AsyncClient(
            base_url="https://egov.invalid/api/2",
            transport=httpx.MockTransport(lambda request: httpx.Response(200, json=law_data)),
        )

    async def fake_prefetch(client):
        return None

    monkeypatch.setattr(mcp_server, "smart_law_lookup", fake_smart_law_lookup)
    monkeypatch.setattr(mcp_server, "get_http_client", fake_http_client)
    monkeypatch.setattr(cache_manager, "prefetch_common_articles", fake_prefetch)
    cache_manager.article_cache.clear()
    yield
    cache_manager.article_cache.clear()


class TestTitleMissingInLawData:
    """題名が取れない応答は、入力名で補わず、空の題名と未確認（False）で返す"""

    @pytest.mark.asyncio
    async def test_find_law_article(self, titleless_law_data):
        async with Client(mcp) as client:
            result = await client.call_tool(
                "find_law_article", {"law_name": "消費税法", "article_number": "1"}
            )
        data = json.loads(result[0].text)
        assert data["actual_law_title"] == ""
        assert data["found_law"] == ""
        assert data["law_title_verified"] is False
        assert "law_title_warning" in data

    @pytest.mark.asyncio
    async def test_batch_find_articles(self, titleless_law_data):
        pairs = [{"law": "消費税法", "article": "1"}]
        async with Client(mcp) as client:
            result = await client.call_tool(
                "batch_find_articles", {"law_article_pairs": json.dumps(pairs, ensure_ascii=False)}
            )
        item = json.loads(result[0].text)["results"][0]
        assert item["actual_law_title"] == ""
        assert item["law_title_verified"] is False


def tax_laws():
    return {
        "消費税法": "昭和六十三年法律第百八号",
        "相続税法": "昭和二十五年法律第七十三号",
        "地方税法": "昭和二十五年法律第二百二十六号",
        "国税通則法": "昭和三十七年法律第六十六号",
        "租税特別措置法": "昭和三十二年法律第二十六号",
    }


class TestRealApiRegression:
    """実 API での回帰テスト（固定対応を使わない経路、題名の食い違い、他のツール）"""

    @pytest.fixture(autouse=True)
    def _clear_cache(self):
        cache_manager.law_lookup_cache.clear()
        yield
        cache_manager.law_lookup_cache.clear()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "law_name, law_num",
        list(tax_laws().items())
        + [
            ("所得税法", "昭和四十年法律第三十三号"),
            ("日本国憲法", "昭和二十一年憲法"),
            ("消費税法施行令", "昭和六十三年政令第三百六十号"),
        ],
    )
    async def test_api_search_without_basic_laws(self, monkeypatch, law_name, law_num):
        """BASIC_LAWS を空にしても、題名検索で正しい法令を選ぶ"""
        monkeypatch.setattr(mcp_server, "BASIC_LAWS", {})
        assert await mcp_server.smart_law_lookup(law_name) == law_num

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "law_name, law_num",
        [
            ("消費税法", "昭和六十三年法律第百八号"),
            # 実 API の候補の先頭が完全一致でない法令（先頭を選ぶと別の法令になる）
            ("所得税法", "昭和四十年法律第三十三号"),
            ("国税通則法", "昭和三十七年法律第六十六号"),
            ("会社法", "平成十七年法律第八十六号"),
        ],
    )
    async def test_prefetch_lookup_uses_same_selection(self, monkeypatch, law_name, law_num):
        """CacheManager._get_law_number も同じ選び方をする"""
        monkeypatch.setattr(mcp_server, "BASIC_LAWS", {})
        async with await mcp_server.get_http_client() as client:
            assert await cache_manager._get_law_number(law_name, client) == law_num

    @pytest.mark.asyncio
    async def test_title_mismatch_is_reported(self):
        """題名が一致しない法令しか見つからないときは、実際の題名と警告を返す（入力名で代用しない）"""
        async with Client(mcp) as client:
            result = await client.call_tool(
                "find_law_article", {"law_name": "消費税", "article_number": "1"}
            )
        data = json.loads(result[0].text)
        assert data["actual_law_title"] != "消費税"
        assert data["actual_law_title"] == data["found_law"]
        assert data["law_title_verified"] is False
        assert "law_title_warning" in data

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "law_name, article, law_num, expected_title",
        [
            ("憲法", "9条第2項", "昭和二十一年憲法", "日本国憲法"),
            (
                "独占禁止法",
                "1",
                "昭和二十二年法律第五十四号",
                "昭和二十二年法律第五十四号（私的独占の禁止及び公正取引の確保に関する法律）",
            ),
            ("会社法", "2条3号の2", "平成十七年法律第八十六号", "会社法"),
        ],
    )
    async def test_find_law_title(self, law_name, article, law_num, expected_title):
        """取得した法令の実際の題名を返し、指定した名前と一致していることを確かめる"""
        async with Client(mcp) as client:
            result = await client.call_tool(
                "find_law_article", {"law_name": law_name, "article_number": article}
            )
        data = json.loads(result[0].text)
        assert data["law_number"] == law_num
        assert data["actual_law_title"] == expected_title
        assert data["law_title_verified"] is True

    @pytest.mark.asyncio
    async def test_batch_find_articles(self):
        pairs = [
            {"law": "消費税法", "article": "1"},
            {"law": "租税特別措置法", "article": "1"},
            {"law": "消費税", "article": "1"},
            {"law": "憲法", "article": "9"},
        ]
        cache_manager.article_cache.clear()
        async with Client(mcp) as client:
            result = await client.call_tool(
                "batch_find_articles", {"law_article_pairs": json.dumps(pairs, ensure_ascii=False)}
            )
        results = json.loads(result[0].text)["results"]
        assert [r["law_number"] for r in results[:2]] == [
            "昭和六十三年法律第百八号",
            "昭和三十二年法律第二十六号",
        ]
        assert [r["actual_law_title"] for r in results[:2]] == ["消費税法", "租税特別措置法"]
        assert all(r["law_title_verified"] for r in results[:2])
        # 題名が一致しない法令しか見つからないときは、batch でも気づけるようにする
        assert results[2]["law_title_verified"] is False
        assert results[2]["actual_law_title"] != "消費税"
        # 別名（憲法 → 日本国憲法）を変換した名前で題名を照合する
        assert results[3]["actual_law_title"] == "日本国憲法"
        assert results[3]["law_title_verified"] is True
