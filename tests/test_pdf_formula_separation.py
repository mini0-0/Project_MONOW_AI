from collections import Counter
from copy import deepcopy
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch

import pymupdf

from scripts.glossary import build_glossary, prepare_definition
from scripts.paths import DEFAULT_PDF
from scripts.pdf_formulas import definition_glyphs
from scripts.pdf_formula_separation import (FORMULA_PATTERNS, FormulaSeparationError,
                                           parse_structure, read_expression, separate_general_formulas)
from scripts.pdf_terms import Term, extract_terms


# PDF/보존 원문을 직접 대조한 기준값. 운영 코드의 탐지 규칙과 독립적으로 작성한다.
REFERENCES = {
    "가계순저축률": ["가계순저축률(%)={가계부문순저축/(가계순처분가능소득+사회적현물이전수취+연금기금의가계순지분증감조정)}×100"],
    "고용률": ["고용률(%)=취업자/15세이상인구×100", "실업률(%)=실업자/경제활동인구×100"],
    "공급탄력성": ["공급량의변화율÷가격의변화율"],
    "국민소득": ["국민소득(NI)=국민총소득-고정자본소모-생산및수입세+보조금=국민순소득-생산및수입세+보조금=국민순소득-순생산및수입세=피용자보수+영업잉여=임금+지대+이자+이윤"],
    "국외저축": ["순대외채무(=대외채무-대외채권)"],
    "국외투자율": ["GNDI=최종소비지출+총저축", "총투자율(%)=국내총투자율(%)+국외투자율(%)",
                "국내총투자율(%)=(국내총투자/GNDI)×100", "국외투자율(%)=(국외투자/GNDI)×100"],
    "기업경기실사지수(BSI)": ["BSI=[(긍정적응답업체수-부정적응답업체수)/전체응답업체수]×100+100"],
    "노동생산성/노동생산성지수": ["노동생산성지수=산출량지수/노동투입량지수×100"],
    "듀레이션": ["(dp/p)=-〔dr/(1+r)〕×D"],
    "레버리지비율/단순기본자본비율": ["[기본자본(Tier1)/총익스포저(난외자산포함)]×100"],
    "레버리지 효과": ["투자레버리지(=총투자액/자기자본)"],
    "마샬의 k": ["MV=PY", "M=1/V×PY=kPY", "MS=Md=kPY"],
    "배당할인모형": ["P0=D1/(r-g)"],
    "본원소득": ["본원소득=피용자보수+영업잉여+순생산및수입세+순수취재산소득"],
    "본원통화": ["민간보유현금+금융기관보유시재금", "통화량=통화승수×본원통화"],
    "부채비율": ["부채비율=[(유동부채+비유동부채)/자기자본]×100"],
    "성장기여도": ["X_1의성장기여도(%p)=X_1의성장기여율×ΣX_n의증감률",
               "X_1의성장기여율(%)=(X_1의증감액/ΣX_n의증감액)×100"],
    "소득교역조건지수": ["{(수출물가지수/수입물가지수)×수출물량지수}"],
    "순안정자금조달비율": ["가용안정자금조달금액(∑부채및자본항목잔액×ASF가중치)/필요안정자금조달금액(∑자산항목잔액×RSF가중치)×100"],
    "스왑레이트": ["(선물환율-현물환율)/현물환율", "내외금리차-스왑레이트"],
    "시뇨리지": ["화폐시뇨리지=화폐액면가-화폐제조비용"],
    "예상손실": ["부도시익스포저(EAD;ExposureatDefault)×예상부도율(PD;ProbabilityofDefault)×부도시손실률(LGD;LossGivenDefault)"],
    "요소비용 국민소득": ["요소비용에의한국민소득=시장가격에의한국민소득-생산및수입세+보조금=피용자보수+영업잉여"],
    "유동비율": ["유동비율=유동자산/유동부채×100"],
    "유동성커버리지비율": ["고유동성자산/향후30일간순현금유출액(현금유출액-현금유입액)×100"],
    "자기자본비율": ["자산(=부채+자기자본)", "자기자본비율=자기자본/총자본×100"],
    "제조업생산능력/가동률지수": ["가동률=생산실적/생산능력×100"],
    "조세부담률": ["조세부담률=(국세+지방세)/경상GDP×100", "국민부담률=(국세+지방세+사회보장기여금)/경상GDP×100"],
    "총부채상환비율(DTI)": ["총부채상환비율=[(해당주택담보대출및기존주택담보대출의연간원리금상환액+기타부채의연간이자상환액)/연소득]×100"],
    "총부채원리금상환비율(DSR)": ["총부채원리금상환비율=(금융회사대출의연간원리금상환액/연소득)×100"],
    "통화승수": ["M(통화)=RB(본원통화)×k(통화승수)", "k=1/{c+(1-c)r}"],
    "현시비교우위지수(RCA)": ["RCAij=(Xij/Xit)/(Xwj/Xwt)"],
    "EC방식": ["MV=PY", "V=PY/M", "dM/M=dY/Y+dP/P-dV/V"],
    "국민부담률": ["조세부담률+사회보장부담률"],
    "신용레버리지": ["민간신용/명목GDP"],
    "한계소비성향": ["△C/△Y"],
}


def compact(text):
    return re.sub(r"\s", "", text)


def walk(node):
    yield node
    for value in node.values():
        if isinstance(value, dict):
            yield from walk(value)
        elif isinstance(value, list):
            for child in value:
                if isinstance(child, dict):
                    yield from walk(child)


class GeneralFormulaTests(unittest.TestCase):
    def test_special_relations_preserve_symbols_and_fraction_structure(self):
        for symbol in ("≒", "≠", "≤", "≥"):
            with self.subTest(symbol=symbol):
                tree = parse_structure(f"π÷(∑x) {symbol} i^*×2")
                self.assertEqual(tree["operator"], symbol)
                self.assertEqual(tree["left"]["type"], "division")
                self.assertEqual(tree["left"]["operator"], "÷")
                self.assertEqual(tree["right"]["operator"], "×")
                self.assertEqual(tree["right"]["left"]["value"], "i^*")

    def test_position_based_superscript_star_and_subscript(self):
        for marker, char, baseline in (("^", "*", 7), ("_", "1", 13)):
            source = f"i{char}×π≒∑x÷2"
            term = Term("synthetic", source, [], 38, 20)
            glyphs = [{"c": c, "index": i, "pdf_page": 38,
                       "bbox": (i * 5, 0, i * 5 + 4, 15),
                       "origin": (i * 5, baseline if i == 1 else 10),
                       "size": 7 if i == 1 else 10, "font": "test"}
                      for i, c in enumerate(source)]
            result = read_expression(term, glyphs, 0, len(source))
            self.assertEqual(result["expression"], f"i{marker}{char}×π≒∑x÷2")
            self.assertEqual(result["superscript_indices"], [1] if marker == "^" else [])
            self.assertEqual(result["subscript_indices"], [1] if marker == "_" else [])

    @classmethod
    def setUpClass(cls):
        cls.original = extract_terms()
        cls.original_dicts = [term.to_dict() for term in cls.original]
        cls.terms = {term.term: term for term in cls.original}
        cls.items, cls.report = separate_general_formulas(DEFAULT_PDF, cls.original)

    def test_36_targets_succeed_with_expected_50_formulas(self):
        self.assertEqual(set(FORMULA_PATTERNS), set(REFERENCES))
        self.assertEqual(self.report["target_term_count"], 36)
        self.assertEqual(self.report["success_term_count"], 36)
        self.assertEqual(self.report["failed_term_count"], 0)
        self.assertEqual(self.report["formula_count"], 50)

    def test_all_expressions_match_original_reference_without_missing_or_duplicate_formulas(self):
        for title, expected in REFERENCES.items():
            with self.subTest(term=title):
                actual = [compact(item["expression"]) for item in self.items[title]]
                self.assertEqual(actual, expected)
                self.assertEqual(len(actual), len(set(actual)))

    def test_original_789_definitions_unchanged(self):
        self.assertEqual([term.to_dict() for term in self.original], self.original_dicts)
        for title, items in self.items.items():
            for item in items:
                start, end = item["source_range"]
                self.assertEqual(item["source_text"], self.terms[title].definition[start:end])

    def test_pdf_glyph_positions_and_character_accounting(self):
        for items in self.items.values():
            for item in items:
                self.assertTrue(item["source_glyphs"])
                self.assertEqual(Counter(compact(item["expression"])),
                                 Counter(compact(item["source_text"])) + Counter("_" * len(item["subscript_indices"])))
                self.assertEqual(item["source_pdf_page"], min(g["pdf_page"] for g in item["source_glyphs"]))
                self.assertEqual(item["source_book_page"], item["source_pdf_page"] - 18)

    def test_multiple_formulas_remain_separate(self):
        self.assertEqual(len(self.items["고용률"]), 2)
        self.assertEqual(len(self.items["국외투자율"]), 4)
        self.assertEqual(len(self.items["마샬의 k"]), 3)
        self.assertEqual(len(self.items["조세부담률"]), 2)
        self.assertEqual(len(self.items["통화승수"]), 2)

    def test_employment_and_unemployment_denominators_not_swapped(self):
        divisions = [[node for node in walk(item["structure"]) if node["type"] == "division"]
                     for item in self.items["고용률"]]
        self.assertEqual(divisions[0][0]["numerator"]["value"], "취업자")
        self.assertEqual(divisions[0][0]["denominator"]["value"], "15세이상인구")
        self.assertEqual(divisions[1][0]["numerator"]["value"], "실업자")
        self.assertEqual(divisions[1][0]["denominator"]["value"], "경제활동인구")

    def test_nested_ratio_negative_and_implicit_product_structure(self):
        rca = [node for node in walk(self.items["현시비교우위지수(RCA)"][0]["structure"])
               if node["type"] == "division"]
        self.assertEqual(len(rca), 3)
        self.assertEqual(rca[0]["denominator"]["type"], "group")
        duration = [node for node in walk(self.items["듀레이션"][0]["structure"]) if node["type"] == "unary"]
        self.assertEqual(duration[0]["operator"], "-")
        multiplier = self.items["통화승수"][1]
        self.assertIn("{c+(1-c)r}", multiplier["expression"])

    def test_physical_subscripts_preserved_without_changing_definition(self):
        items = self.items["성장기여도"]
        self.assertEqual([len(item["subscript_indices"]) for item in items], [3, 3])
        self.assertIn("X_1", items[0]["expression"])
        self.assertIn("ΣX_n", items[0]["expression"])
        self.assertNotIn("X_1", self.terms["성장기여도"].definition)

    def test_actual_formula_pages_can_follow_term_start_page(self):
        for title, expected in [("국민소득", 57), ("국외저축", 61),
                                ("노동생산성/노동생산성지수", 104), ("통화승수", 361)]:
            self.assertEqual(self.items[title][0]["source_pdf_page"], expected)

    def test_chained_equation_preserved_and_examples_not_invented(self):
        self.assertEqual(self.items["국민소득"][0]["expression"].count("="), 5)
        self.assertNotIn("10억원", self.items["레버리지 효과"][0]["expression"])
        self.assertNotIn("9%", " ".join(item["expression"] for item in self.items["EC방식"]))
        self.assertEqual(self.items["국민부담률"][0]["expression"], "조세부담률+사회보장부담률")

    def test_out_of_scope_formula_examples_and_empty_terms_not_added(self):
        for title in ("단리/복리", "전월세 전환율", "모기지대출", "주가수익비율(PER)"):
            self.assertNotIn(title, self.items)

    def test_partial_boundary_failure_withholds_entire_term(self):
        with patch.dict(FORMULA_PATTERNS, {"고용률": (FORMULA_PATTERNS["고용률"][0], r"없는(?P<expression>수식)")}):
            items, report = separate_general_formulas(DEFAULT_PDF, [self.terms["고용률"]])
        self.assertEqual(items["고용률"], [])
        self.assertEqual(report["failed_term_count"], 1)
        self.assertEqual(report["terms"][0]["status"], "needs_review")

    def test_duplicate_regions_rejected(self):
        first = FORMULA_PATTERNS["고용률"][0]
        with patch.dict(FORMULA_PATTERNS, {"고용률": (first, first)}):
            items, report = separate_general_formulas(DEFAULT_PDF, [self.terms["고용률"]])
        self.assertEqual(items["고용률"], [])
        self.assertIn("중복", report["terms"][0]["reason"])

    def test_unsupported_edition_not_guessed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "unknown.pdf"
            path.write_bytes(b"not-the-reviewed-pdf")
            with self.assertRaisesRegex(FormulaSeparationError, "SHA-256"):
                separate_general_formulas(path, self.original)

    def test_incomplete_brackets_and_operands_rejected(self):
        for expression in ("a/", "a/(b]", "a×", "a=()", "a+(b", "a==b"):
            with self.subTest(expression=expression), self.assertRaises(FormulaSeparationError):
                parse_structure(expression)

    def test_division_and_times_100_precedence(self):
        outside = parse_structure("x=a/b×100")["right"]
        inside = parse_structure("x=a/(b×100)")["right"]
        self.assertEqual(outside["operator"], "×")
        self.assertEqual(outside["left"]["type"], "division")
        self.assertEqual(inside["type"], "division")
        self.assertEqual(inside["denominator"]["value"]["operator"], "×")

    def test_coordinate_order_disagreement_rejected(self):
        term = self.terms["고용률"]
        with pymupdf.open(DEFAULT_PDF) as document:
            glyphs = deepcopy(definition_glyphs(document, term))
        start, end = self.items[term.term][0]["source_range"]
        selected = [glyph for glyph in glyphs if start <= glyph["index"] < end]
        selected[2]["origin"] = (0, selected[2]["origin"][1])
        with self.assertRaisesRegex(FormulaSeparationError, "가로 글자 순서"):
            read_expression(term, glyphs, start, end)

    def test_final_expression_schema_and_definition_preservation(self):
        glossary, report, _ = build_glossary(targets=("고용률",))
        record = glossary[0]
        self.assertEqual(record["definition"], prepare_definition(self.terms["고용률"].definition))
        self.assertEqual(len(record["formulas"]), 2)
        self.assertEqual(set(record["formulas"][0]), {"expression"})
        self.assertNotIn("type", record["formulas"][0])
        self.assertNotIn("text", record["formulas"][0])
        self.assertEqual(report["summary"]["general_formula_success_count"], 1)

    def test_uncertain_separation_propagates_needs_review_to_glossary(self):
        with patch.dict(FORMULA_PATTERNS, {"고용률": (r"없는(?P<expression>수식)",)}):
            glossary, report, _ = build_glossary(targets=("고용률",))
        self.assertEqual(glossary[0]["review_status"], "needs_review")
        self.assertEqual(glossary[0]["formulas"], [])
        self.assertEqual(report["summary"]["general_formula_failed_count"], 1)
        self.assertIn("formula_separation_uncertain", [issue["code"] for issue in report["review_required"][0]["issues"]])


if __name__ == "__main__":
    unittest.main(verbosity=2)
