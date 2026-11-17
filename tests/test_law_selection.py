#!/usr/bin/env python3
"""
法令の選択と条文抽出の回帰テスト

- 題名検索の候補から、題名が完全一致する法令を選ぶこと
  （消費税法が「…消費税法の一部を改正する等の法律」に化けない）
- 条文は本則（MainProvision）から取り出し、目次・附則・改正規定の条を返さないこと
"""

import base64
import json
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from fastmcp import Client
from fastmcp.exceptions import ToolError

import mcp_server
from mcp_server import (
    BASIC_LAWS,
    cache_manager,
    extract_article,
    find_article_in_law_xml,
    format_article_label,
    get_law_title,
    kanji_to_arabic,
    law_title_matches,
    mcp,
    normalize_article_number,
    parse_article_reference,
    parse_law_xml,
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


def test_basic_laws_include_tax_laws():
    for name, num in tax_laws().items():
        assert BASIC_LAWS.get(name) == num


class TestArticleReference:
    """条番号の解釈"""

    @pytest.mark.parametrize(
        "text, article, paragraph, item",
        [
            ("192", "192", None, None),
            ("第192条", "192", None, None),
            ("325条の3", "325_3", None, None),
            ("第9条第2項", "9", "2", None),
            ("第2条第1項第3号", "2", "1", "3"),
            ("第二条第三号", "2", None, "3"),
            ("第三百二十五条の三", "325_3", None, None),
            ("１９２", "192", None, None),
            ("10条の2の3", "10_2_3", None, None),
            ("2条3号の2", "2", None, "3_2"),
            ("第2条第1項第3号の2", "2", "1", "3_2"),
            ("第二条第三号の二", "2", None, "3_2"),
            ("第2条 第1項　第3号", "2", "1", "3"),
            ("325の3", "325_3", None, None),
        ],
    )
    def test_parse(self, text, article, paragraph, item):
        assert parse_article_reference(text) == {
            "article": article,
            "paragraph": paragraph,
            "item": item,
        }

    def test_kanji_numbers(self):
        assert kanji_to_arabic("百九十二") == 192
        assert kanji_to_arabic("三百二十五") == 325
        assert kanji_to_arabic("千二十") == 1020
        assert normalize_article_number("第九条第二項") == "第9条第2項"

    @pytest.mark.parametrize(
        "text",
        [
            "総則",
            "条",
            "1項",
            "12項",
            "192-2",
            "192~193",
            "第192条, 第193条",
            "709条の",
            "民法192条",
            "前文",
            "附則第1条",
            "第2条第1項第3号イ",
            "192条第",
            "1" * 5000,
            "九" * 5000,
        ],
    )
    def test_uninterpretable_input_is_invalid(self, text):
        """解釈できない指定は、別の条に化けさせない"""
        assert parse_article_reference(text) is None

    def test_article_label(self):
        assert format_article_label("325_3") == "第325条の3"


# 目次・本則・附則がそれぞれ「第一条」を持つ法令 XML（e-Gov 法令標準 XML の構造を簡略化）
SAMPLE_LAW_XML = """<?xml version="1.0" encoding="UTF-8"?>
<Law>
  <LawNum>昭和三十二年法律第二十六号</LawNum>
  <LawBody>
    <LawTitle>テスト特別措置法</LawTitle>
    <TOC>
      <TOCChapter Num="1">
        <ChapterTitle>第一章　総則</ChapterTitle>
        <ArticleRange>（第一条―第二条）</ArticleRange>
      </TOCChapter>
    </TOC>
    <MainProvision>
      <Chapter Num="1">
        <ChapterTitle>第一章　総則</ChapterTitle>
        <Article Num="1">
          <ArticleCaption>（趣旨）</ArticleCaption>
          <ArticleTitle>第一条</ArticleTitle>
          <Paragraph Num="1">
            <ParagraphNum/>
            <ParagraphSentence>
              <Sentence Num="1">この法律は、当分の間、<Ruby>租税<Rt>そぜい</Rt></Ruby>の特例を定めるものとする。</Sentence>
            </ParagraphSentence>
          </Paragraph>
        </Article>
        <Article Num="2">
          <ArticleCaption>（定義）</ArticleCaption>
          <ArticleTitle>第二条</ArticleTitle>
          <Paragraph Num="1">
            <ParagraphNum/>
            <ParagraphSentence>
              <Sentence Num="1">この法律において、次の各号に掲げる用語の意義は、当該各号に定めるところによる。</Sentence>
            </ParagraphSentence>
            <Item Num="1">
              <ItemTitle>一</ItemTitle>
              <ItemSentence>
                <Column Num="1"><Sentence Num="1">内国法人</Sentence></Column>
                <Column Num="2"><Sentence Num="1">国内に本店を有する法人をいう。</Sentence></Column>
              </ItemSentence>
            </Item>
            <Item Num="1_2">
              <ItemTitle>一の二</ItemTitle>
              <ItemSentence>
                <Column Num="1"><Sentence Num="1">内国普通法人</Sentence></Column>
                <Column Num="2"><Sentence Num="1">内国法人のうち普通法人をいう。</Sentence></Column>
              </ItemSentence>
            </Item>
            <Item Num="2">
              <ItemTitle>二</ItemTitle>
              <ItemSentence>
                <Column Num="1"><Sentence Num="1">外国法人</Sentence></Column>
                <Column Num="2"><Sentence Num="1">内国法人以外の法人をいう。</Sentence></Column>
              </ItemSentence>
            </Item>
          </Paragraph>
          <Paragraph Num="2">
            <ParagraphNum>２</ParagraphNum>
            <ParagraphSentence>
              <Sentence Num="1">前項の規定は、次条において準用する。</Sentence>
            </ParagraphSentence>
          </Paragraph>
        </Article>
        <Article Num="2_2">
          <ArticleTitle>第二条の二</ArticleTitle>
          <Paragraph Num="1">
            <ParagraphNum/>
            <ParagraphSentence>
              <Sentence Num="1">削除</Sentence>
            </ParagraphSentence>
          </Paragraph>
        </Article>
        <Article Num="3">
          <ArticleCaption>（他の法律の改正）</ArticleCaption>
          <ArticleTitle>第三条</ArticleTitle>
          <Paragraph Num="1">
            <ParagraphNum/>
            <ParagraphSentence>
              <Sentence Num="1">別法の一部を次のように改正する。</Sentence>
            </ParagraphSentence>
            <AmendProvision>
              <AmendProvisionSentence>
                <Sentence Num="1">第七条を次のように改める。</Sentence>
              </AmendProvisionSentence>
              <NewProvision>
                <Article Num="7">
                  <ArticleTitle>第七条</ArticleTitle>
                  <Paragraph Num="1">
                    <ParagraphNum/>
                    <ParagraphSentence>
                      <Sentence Num="1">改正規定の中にだけある条。</Sentence>
                    </ParagraphSentence>
                  </Paragraph>
                </Article>
              </NewProvision>
            </AmendProvision>
          </Paragraph>
        </Article>
        <Article Num="4:6">
          <ArticleTitle>第四条から第六条まで</ArticleTitle>
          <Paragraph Num="1">
            <ParagraphNum/>
            <ParagraphSentence>
              <Sentence Num="1">削除</Sentence>
            </ParagraphSentence>
          </Paragraph>
        </Article>
      </Chapter>
    </MainProvision>
    <SupplProvision>
      <SupplProvisionLabel>附　則</SupplProvisionLabel>
      <Article Num="1">
        <ArticleTitle>第一条</ArticleTitle>
        <Paragraph Num="1">
          <ParagraphNum/>
          <ParagraphSentence>
            <Sentence Num="1">この法律は、公布の日から施行する。</Sentence>
          </ParagraphSentence>
        </Paragraph>
      </Article>
      <Article Num="3">
        <ArticleTitle>第三条</ArticleTitle>
        <Paragraph Num="1">
          <ParagraphNum/>
          <ParagraphSentence>
            <Sentence Num="1">附則にだけある条。</Sentence>
          </ParagraphSentence>
        </Paragraph>
      </Article>
    </SupplProvision>
  </LawBody>
</Law>
"""


@pytest.fixture
def sample_root():
    encoded = base64.b64encode(SAMPLE_LAW_XML.encode("utf-8")).decode("ascii")
    root = parse_law_xml(encoded)
    assert root is not None
    return root


class TestStructuredArticleExtraction:
    """本則から条文を取り出す"""

    def test_article_1_is_main_provision_not_toc_or_suppl(self, sample_root):
        result = find_article_in_law_xml(sample_root, "1")
        assert result["status"] == "found"
        assert (
            result["text"]
            == "（趣旨）\n第一条　この法律は、当分の間、租税の特例を定めるものとする。"
        )
        assert "公布の日から施行" not in result["text"]
        assert "第一条―第二条" not in result["text"]

    def test_full_article_with_items_and_paragraphs(self, sample_root):
        text = find_article_in_law_xml(sample_root, "第2条")["text"]
        assert text.splitlines() == [
            "（定義）",
            "第二条　この法律において、次の各号に掲げる用語の意義は、当該各号に定めるところによる。",
            "一　内国法人　国内に本店を有する法人をいう。",
            "一の二　内国普通法人　内国法人のうち普通法人をいう。",
            "二　外国法人　内国法人以外の法人をいう。",
            "２　前項の規定は、次条において準用する。",
        ]

    def test_paragraph(self, sample_root):
        text = find_article_in_law_xml(sample_root, "2条第2項")["text"]
        assert text.splitlines() == [
            "（定義）",
            "第二条第二項",
            "２　前項の規定は、次条において準用する。",
        ]

    def test_item(self, sample_root):
        text = find_article_in_law_xml(sample_root, "第2条第1項第2号")["text"]
        assert text.splitlines() == [
            "（定義）",
            "第二条第一項",
            "二　外国法人　内国法人以外の法人をいう。",
        ]

    def test_branch_item_is_not_confused_with_base_item(self, sample_root):
        """「1号の2」は Item Num="1_2"。一号を返さない"""
        text = find_article_in_law_xml(sample_root, "2条1号の2")["text"]
        assert text.splitlines()[-1] == "一の二　内国普通法人　内国法人のうち普通法人をいう。"
        assert find_article_in_law_xml(sample_root, "2条1号の5")["status"] == "item_not_found"

    def test_branch_article(self, sample_root):
        assert find_article_in_law_xml(sample_root, "2条の2")["text"] == "第二条の二　削除"

    def test_article_only_in_suppl_provision_is_not_found(self, sample_root):
        assert find_article_in_law_xml(sample_root, "8")["status"] == "article_not_found"
        assert find_article_in_law_xml(sample_root, "附則第3条")["status"] == "invalid_reference"

    def test_article_quoted_in_amend_provision_is_excluded(self, sample_root):
        """改正規定の中に引用された第七条は、この法令の条ではない"""
        assert find_article_in_law_xml(sample_root, "7")["status"] == "article_not_found"
        text = find_article_in_law_xml(sample_root, "3")["text"]
        assert text.startswith("（他の法律の改正）\n第三条　別法の一部を次のように改正する。")

    def test_deleted_article_range(self, sample_root):
        for num in ("4", "5", "6"):
            assert find_article_in_law_xml(sample_root, num)["text"] == "第四条から第六条まで　削除"

    def test_invalid_reference(self, sample_root):
        assert find_article_in_law_xml(sample_root, "1項")["status"] == "invalid_reference"

    def test_missing_paragraph_and_item(self, sample_root):
        assert find_article_in_law_xml(sample_root, "1条第3項")["status"] == "paragraph_not_found"
        assert find_article_in_law_xml(sample_root, "2条第1項第9号")["status"] == "item_not_found"

    def test_parse_law_xml_rejects_non_xml(self):
        """XML として解析できない応答は、推測した条文を返さずエラーにする"""
        for law_full_text in ({"not": "xml"}, "not base64 xml"):
            assert parse_law_xml(law_full_text) is None
            with pytest.raises(ToolError):
                extract_article(law_full_text, "1")


class TestRealTaxLaws:
    """実 API での回帰テスト（申告のあった税法）"""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "law_name, law_num, expected_text",
        [
            ("消費税法", "昭和六十三年法律第百八号", "消費税について、課税の対象"),
            ("相続税法", "昭和二十五年法律第七十三号", "相続税及び贈与税について"),
            ("地方税法", "昭和二十五年法律第二百二十六号", "地方団体　道府県又は市町村をいう。"),
            ("国税通則法", "昭和三十七年法律第六十六号", "国税についての基本的な事項"),
            ("租税特別措置法", "昭和三十二年法律第二十六号", "その他の内国税を軽減し"),
            ("所得税法", "昭和四十年法律第三十三号", "所得税について、納税義務者"),
        ],
    )
    async def test_article_1(self, law_name, law_num, expected_text):
        cache_manager.law_lookup_cache.clear()
        async with Client(mcp) as client:
            result = await client.call_tool(
                "find_law_article", {"law_name": law_name, "article_number": "1"}
            )
        data = json.loads(result[0].text)
        assert data["law_number"] == law_num
        assert data["actual_law_title"] == law_name
        assert data["law_title_verified"] is True
        assert data["matches_found"] == 1
        assert expected_text in data["articles"][0]
        assert "附則" not in data["articles"][0]

    @pytest.mark.asyncio
    async def test_corporate_tax_act_article_2_is_main_provision(self):
        """法人税法第2条（不具合 3 の報告例）は、附則ではなく本則の定義を返す"""
        cache_manager.law_lookup_cache.clear()
        async with Client(mcp) as client:
            result = await client.call_tool(
                "find_law_article", {"law_name": "法人税法", "article_number": "2"}
            )
        data = json.loads(result[0].text)
        assert data["law_number"] == "昭和四十年法律第三十四号"
        assert data["matches_found"] == 1
        text = data["articles"][0]
        assert text.startswith("（定義）\n第二条　この法律において、次の各号に掲げる用語の意義は")
        assert "から施行する" not in text


class TestSecurity:
    """外部（e-Gov API）から受け取る XML と、利用者が入力する条番号の扱い"""

    def _encode(self, xml: str) -> str:
        return base64.b64encode(xml.encode("utf-8")).decode("ascii")

    def test_external_entity_is_not_resolved(self, tmp_path):
        """XXE: 外部実体でローカルファイルを読み込ませない（ElementTree は実体を含む XML を解析しない）"""
        secret = tmp_path / "secret.txt"
        secret.write_text("SECRET-CONTENT", encoding="utf-8")
        xml = (
            f'<?xml version="1.0"?><!DOCTYPE Law [<!ENTITY xxe SYSTEM "{secret.as_uri()}">]>'
            "<Law><LawBody><MainProvision><Article Num=\"1\"><ArticleTitle>第一条</ArticleTitle>"
            "<Paragraph Num=\"1\"><ParagraphNum/><ParagraphSentence><Sentence>&xxe;</Sentence>"
            "</ParagraphSentence></Paragraph></Article></MainProvision></LawBody></Law>"
        )
        assert parse_law_xml(self._encode(xml)) is None
        with pytest.raises(ToolError) as exc_info:
            extract_article(self._encode(xml), "1")
        assert "SECRET-CONTENT" not in str(exc_info.value)

    def test_entity_expansion_bomb_is_rejected(self):
        """
        billion laughs: 実体の多重展開で落ちない（解析を失敗させて None を返す）

        標準ライブラリの ElementTree は、同梱の Expat（2.4.1 以上）の増幅攻撃への対策に依存する。
        対策のない古い Expat の環境では、このテストが失敗する（skip しない）。
        """
        # a{i} は a{i-1} を 10 回参照する（展開すると 10**9 個の "lol"）
        entities = '<!ENTITY a0 "lol">' + "".join(
            f'<!ENTITY a{i} "{f"&a{i - 1};" * 10}">' for i in range(1, 10)
        )
        xml = f'<?xml version="1.0"?><!DOCTYPE Law [{entities}]><Law>&a9;</Law>'
        assert parse_law_xml(self._encode(xml)) is None

    @pytest.mark.parametrize(
        "text",
        [
            "1'] | //*[@Num='2",
            "1\"]//Article[@Num=\"2",
            "1 or 1=1",
            "../../etc/passwd",
            "<Article Num='1'/>",
            "1" * 5000,
            "九" * 5000,
        ],
    )
    def test_crafted_article_number_does_not_reach_xpath(self, sample_root, text):
        """条番号はホワイトリストに全体一致したものだけを検索に使う。細工した文字列で別の要素を引けない"""
        result = find_article_in_law_xml(sample_root, text)
        assert result["status"] == "invalid_reference"
        assert result["text"] is None


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
        "law_name, article, law_num, expected_title, expected_text",
        [
            (
                "憲法",
                "9条第2項",
                "昭和二十一年憲法",
                "日本国憲法",
                "陸海空軍その他の戦力は、これを保持しない",
            ),
            (
                "独占禁止法",
                "1",
                "昭和二十二年法律第五十四号",
                "昭和二十二年法律第五十四号（私的独占の禁止及び公正取引の確保に関する法律）",
                "私的独占、不当な取引制限",
            ),
            ("会社法", "2条3号の2", "平成十七年法律第八十六号", "会社法", "子会社等"),
            ("商法", "1", "明治三十二年法律第四十八号", "商法", "商人の営業、商行為その他商事"),
        ],
    )
    async def test_find_law_article(
        self, law_name, article, law_num, expected_title, expected_text
    ):
        async with Client(mcp) as client:
            result = await client.call_tool(
                "find_law_article", {"law_name": law_name, "article_number": article}
            )
        data = json.loads(result[0].text)
        assert data["law_number"] == law_num
        assert data["actual_law_title"] == expected_title
        assert data["law_title_verified"] is True
        assert expected_text in data["articles"][0]

    @pytest.mark.asyncio
    async def test_branch_item_does_not_return_base_item(self):
        async with Client(mcp) as client:
            result = await client.call_tool(
                "find_law_article", {"law_name": "会社法", "article_number": "2条3号の2"}
            )
        text = json.loads(result[0].text)["articles"][0]
        assert "三の二" in text
        assert "子会社　会社がその総株主" not in text

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
        assert "公布の日から施行する" not in results[1]["found_article"]
        assert "当分の間" in results[1]["found_article"]
        # 題名が一致しない法令しか見つからないときは、batch でも気づけるようにする
        assert results[2]["law_title_verified"] is False
        assert results[2]["actual_law_title"] != "消費税"
        # 別名（憲法 → 日本国憲法）を変換した名前で題名を照合する
        assert results[3]["actual_law_title"] == "日本国憲法"
        assert results[3]["law_title_verified"] is True

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "article, expected_suggestion",
        [
            ("附則第1条", "Could not interpret the article number"),
            ("999", "was not found in the main provision"),
            ("9条第5項", "the specified paragraph (項) does not exist"),
            ("9条第1項第1号", "the specified subparagraph (号) does not exist"),
        ],
    )
    async def test_not_found_returns_suggestion(self, article, expected_suggestion):
        """見つからない指定は、別の箇所に化けさせず、理由を suggestion で返す"""
        async with Client(mcp) as client:
            result = await client.call_tool(
                "find_law_article", {"law_name": "憲法", "article_number": article}
            )
        data = json.loads(result[0].text)
        assert data["matches_found"] == 0
        assert data["articles"] == []
        assert expected_suggestion in data["suggestion"]
