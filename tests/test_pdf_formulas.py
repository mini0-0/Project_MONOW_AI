from dataclasses import replace
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET

import pymupdf

from scripts.paths import LEGACY_DIR, PROJECT_ROOT

from scripts.pdf_formulas import (PDF_SHA256, REGIONS, FormulaRestorationError, definition_glyphs,
                          enrich_formulas, linear_nodes, render_text, restore_region,
                          write_formula_results)
from scripts.pdf_terms import DEFAULT_PDF, TARGETS, extract_terms


# PDF 원본 화면을 직접 대조하여 작성한 기준. 추출 함수의 출력에서 생성하지 않는다.
REFERENCE_EQUATIONS = {
    "경제성장률": ["경제성장률(%) = (금년 실질GDP - 전년 실질GDP) / (전년 실질GDP)"],
    "경제활동인구/비경제활동인구/경제활동참가율": [
        "경제활동참가율 = (경제활동인구(취업자 + 실업자)) / (15세 이상 인구) ×100"],
    "금리평가이론": ["￦1억 × (1+ i) = ((￦1억) / (S)) × (1+ i^*) × F",
                  "(F-S) / (S) ≒ i-i^*"],
    "단위노동비용": ["단위노동비용=(노동비용)/(총산출량)=(시간당 노동비용×총노동시간)/(총산출량)"
                 "=(시간당 노동비용)/(총산출량/총노동시간)=(시간당 노동비용)/(노동생산성)"],
    "매출액영업이익률": ["매출액영업이익률=(영업손익)/(매출액)×100="
                    "((매출총손익*)/(매출액)×(영업손익*)/(매출총손익))×100"],
    "생산확산지수": ["생산확산지수=(증가 업종수+(보합*업종수×0.5))/(전체 업종 수)×100"],
    "소비자동향지수(CSI)": ["CSI=((매우긍정×1.0+다소긍정×0.5-다소부정×0.5-매우부정×1.0))"
                      "/(전체 응답 가구 수)×100+100"],
    "테일러 준칙(Taylor’s Rule)": ["i_t=r_t^*+π_t+0.5(π_t-π_t^*)+0.5y_t"],
}


REFERENCE_FRACTIONS = {
    "경제성장률": [("금년 실질GDP - 전년 실질GDP", "전년 실질GDP")],
    "경제활동인구/비경제활동인구/경제활동참가율": [("경제활동인구(취업자 + 실업자)", "15세 이상 인구")],
    "금리평가이론": [("￦1억", "S"), ("F-S", "S")],
    "단위노동비용": [("노동비용", "총산출량"), ("시간당 노동비용×총노동시간", "총산출량"),
                 ("시간당 노동비용", "총산출량/총노동시간"), ("시간당 노동비용", "노동생산성")],
    "매출액영업이익률": [("영업손익", "매출액"), ("매출총손익*", "매출액"), ("영업손익*", "매출총손익")],
    "생산확산지수": [("증가 업종수 + (보합*업종수×0.5)", "전체 업종 수")],
    "소비자동향지수(CSI)": [("(매우긍정 × 1.0 + 다소긍정 × 0.5 - 다소부정 × 0.5 - 매우부정 × 1.0)",
                        "전체 응답 가구 수")],
    "테일러 준칙(Taylor’s Rule)": [],
}


def compact(text):
    return re.sub(r"\s", "", text)


def walk(node):
    yield node
    if node["type"] == "sequence":
        for child in node["children"]:
            yield from walk(child)
    elif node["type"] == "fraction":
        yield from walk(node["numerator"])
        yield from walk(node["denominator"])


class FormulaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.original = extract_terms()
        cls.records, cls.report = enrich_formulas(DEFAULT_PDF, cls.original)
        cls.by_title = {record["term"]: record for record in cls.records}

    def test_eight_terms_nine_equations_thirteen_fractions(self):
        self.assertEqual(self.report["restored_term_count"], 8)
        self.assertEqual(self.report["equation_count"], 9)
        self.assertEqual(self.report["fraction_count"], 13)
        self.assertEqual({r["term"] for r in self.records if r["formulas"]}, set(REGIONS))

    def test_equation_order_operators_and_numbers_match_visual_references(self):
        for title, expected in REFERENCE_EQUATIONS.items():
            with self.subTest(term=title):
                self.assertEqual([compact(item["text"]) for item in self.by_title[title]["formulas"]],
                                 [compact(text) for text in expected])

    def test_each_numerator_and_denominator_match_source_reference(self):
        for title, expected in REFERENCE_FRACTIONS.items():
            with self.subTest(term=title):
                fractions = [node for item in self.by_title[title]["formulas"]
                             for node in walk(item["ast"]) if node["type"] == "fraction"]
                actual = [(compact(render_text(node["numerator"])), compact(render_text(node["denominator"])))
                          for node in fractions]
                self.assertEqual(actual, [(compact(n), compact(d)) for n, d in expected])

    def test_taylor_scripts_and_all_five_pi_glyphs_restored(self):
        record = self.by_title["테일러 준칙(Taylor’s Rule)"]
        self.assertEqual(record["definition"].count("\uf0bc"), 5)
        self.assertNotIn("\uf0bc", record["definition_with_formulas"])
        self.assertEqual(record["definition_with_formulas"].count("π"), 5)
        self.assertIn("π_t^*: 물가목표, y_t: GDP갭률)", record["definition_with_formulas"])
        variables = [node for node in walk(record["formulas"][0]["ast"]) if node["type"] == "variable"]
        self.assertEqual([(n["name"], n.get("subscript"), n.get("superscript")) for n in variables],
                         [("i", "t", None), ("r", "t", "*"), ("π", "t", None),
                          ("π", "t", None), ("π", "t", "*"), ("y", "t", None)])

    def test_original_789_records_and_input_objects_preserved(self):
        baseline = json.loads(LEGACY_DIR.joinpath("all_terms_raw.json").read_text())
        self.assertEqual([term.to_dict() for term in self.original], baseline)
        for record, term in zip(self.records, self.original):
            for key, value in term.to_dict().items():
                self.assertEqual(record[key], value)

    def test_remaining_781_terms_and_three_term_selection_unchanged(self):
        unchanged = [record for record in self.records if not record["formulas"]]
        self.assertEqual(len(unchanged), 781)
        for record in unchanged:
            self.assertEqual(record["definition_with_formulas"], record["definition"])
        selected = extract_terms(targets=TARGETS)
        enriched, report = enrich_formulas(DEFAULT_PDF, selected)
        self.assertEqual(report["restored_term_count"], 0)
        self.assertEqual([r["term"] for r in enriched], list(TARGETS))

    def test_characters_outside_selected_regions_preserved(self):
        for title in REGIONS:
            record = self.by_title[title]
            parts = record["formulas"] + record["formula_annotations"]
            used = {index for part in parts for index in part["source_indices"]}
            expected = "".join(c for index, c in enumerate(record["definition"]) if index not in used)
            remainder = record["definition_with_formulas"]
            for part in parts:
                self.assertEqual(remainder.count(part["text"]), 1)
                remainder = remainder.replace(part["text"], "", 1)
            self.assertEqual(compact(remainder), compact(expected), title)

    def test_mathml_fraction_and_script_structure(self):
        total_fractions = 0
        for record in self.records:
            for item in record["formulas"] + record["formula_annotations"]:
                root = ET.fromstring("<math>" + item["mathml"] + "</math>")
                total_fractions += len(root.findall(".//mfrac"))
        self.assertEqual(total_fractions, 13)
        taylor = self.by_title["테일러 준칙(Taylor’s Rule)"]["formulas"][0]["mathml"]
        self.assertEqual(len(ET.fromstring(taylor).findall(".//msubsup")), 2)

    def test_no_unprinted_times_100_and_footnotes_retained(self):
        self.assertNotIn("100", self.by_title["경제성장률"]["formulas"][0]["text"])
        record = self.by_title["매출액영업이익률"]
        self.assertIn("* 매출총소익 = 매출액 - 매출원가", record["definition_with_formulas"])
        self.assertIn("** 영업손익", record["definition_with_formulas"])
        self.assertIn("* 보합: -0.05% < 증감률 < 0.05%",
                      self.by_title["생산확산지수"]["definition_with_formulas"])

    def test_wrong_edition_rejected_before_generating_guessed_content(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "other.pdf"
            path.write_bytes(DEFAULT_PDF.read_bytes() + b"\n% modified\n")
            with self.assertRaisesRegex(FormulaRestorationError, "SHA-256"):
                enrich_formulas(path, self.original)

    def test_changed_definition_is_rejected(self):
        term = next(term for term in self.original if term.term == "경제성장률")
        with self.assertRaisesRegex(FormulaRestorationError, "원본 정의"):
            enrich_formulas(DEFAULT_PDF, [replace(term, definition=term.definition + "변경")])

    def test_missing_denominator_rejected(self):
        term = next(term for term in self.original if term.term == "경제성장률")
        region = REGIONS[term.term][0]
        with pymupdf.open(DEFAULT_PDF) as document:
            glyphs = definition_glyphs(document, term)
            without_denominator = [g for g in glyphs if not (
                g["pdf_page"] == 33 and g["bbox"][1] > 655 and g["bbox"][3] < 670)]
            with self.assertRaisesRegex(FormulaRestorationError, "분자 또는 분모"):
                restore_region(document[32], region, without_denominator)

    def test_missing_fraction_bar_rejected(self):
        term = next(term for term in self.original if term.term == "경제성장률")
        with pymupdf.open(DEFAULT_PDF) as document:
            glyphs = definition_glyphs(document, term)
            class NoBars:
                def get_drawings(self):
                    return []
            with self.assertRaisesRegex(FormulaRestorationError, "분수선"):
                restore_region(NoBars(), REGIONS[term.term][0], glyphs)

    def test_private_use_mapping_requires_verified_math_font(self):
        glyph = {"c": "\uf0bc", "font": "OtherFont", "size": 10.5,
                 "bbox": (0, 0, 5, 10), "origin": (0, 8), "index": 0}
        with self.assertRaisesRegex(FormulaRestorationError, "전용영역"):
            linear_nodes([glyph], map_pi=True)

    def test_json_and_html_export_preserve_existing_raw_file(self):
        with tempfile.TemporaryDirectory() as directory:
            sentinel = Path(directory) / "all_terms_raw.json"
            sentinel.write_text("원본 유지")
            write_formula_results(directory, self.records, self.report)
            self.assertEqual(sentinel.read_text(), "원본 유지")
            saved = json.loads((Path(directory) / "all_terms_with_formulas.json").read_text())
            self.assertEqual(saved, self.records)
            html = (Path(directory) / "all_terms_formula_comparison.html").read_text()
            self.assertIn("<mfrac>", html)
            self.assertEqual(len(list((Path(directory) / "all_terms_formula_evidence").glob("*.png"))), 11)

    def test_cli_restore_option_saves_all_789_without_overwriting_raw(self):
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run(
                [sys.executable, str(PROJECT_ROOT / "scripts" / "pdf_terms.py"),
                 "--restore-formulas", "--output-dir", directory],
                text=True, capture_output=True, check=True)
            summary = json.loads(result.stdout)
            self.assertEqual(summary["record_count"], 789)
            self.assertEqual(summary["restored_term_count"], 8)
            self.assertFalse((Path(directory) / "all_terms_raw.json").exists())
            self.assertEqual(hashlib.sha256(DEFAULT_PDF.read_bytes()).hexdigest(), PDF_SHA256)


if __name__ == "__main__":
    unittest.main(verbosity=2)
