from dataclasses import replace
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest

import pymupdf

from scripts.paths import LEGACY_DIR, PROJECT_ROOT

from scripts.pdf_terms import DEFAULT_PDF, TARGETS, clean_definition, extract_terms
from scripts.pdf_validation import extract_toc, title_key, validate_extraction, write_results


class AllTermsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.terms = extract_terms()
        cls.report = validate_extraction(DEFAULT_PDF, cls.terms)

    def test_full_count_matches_independent_toc_leader_count(self):
        with pymupdf.open(DEFAULT_PDF) as document:
            independent_count = sum(len(re.findall(r"·{2,}\s*\d+\s*$", page.get_text(), re.M))
                                    for page in document[3:17])
        self.assertEqual(independent_count, 789)
        self.assertEqual(len(self.terms), independent_count)
        self.assertEqual(self.report["summary"]["difference_from_book_title"], -11)

    def test_all_toc_titles_pages_and_order(self):
        expected = [(title_key(entry["term"]), entry["source_book_page"])
                    for entry in self.report["toc_entries"]]
        actual = [(title_key(term.term), term.source_book_page) for term in self.terms]
        self.assertEqual(actual, expected)
        self.assertEqual(self.report["missing_terms"], [])
        self.assertEqual(self.report["unexpected_terms"], [])
        self.assertEqual(self.report["toc_parse_issues"], [])

    def test_multiline_toc_entries_and_quote_difference_preserved(self):
        expected = "경영실태평가/은행경영실태 평가등급제도"
        self.assertTrue(any(title_key(entry["term"]) == title_key(expected)
                            for entry in self.report["toc_entries"]))
        quote_differences = [entry for entry in self.report["presentation_differences"]
                             if entry["kind"] == "quote_variant"]
        self.assertEqual(len(quote_differences), 1)
        self.assertEqual(quote_differences[0]["toc_term"], "트리핀 딜레마(Triffin's Dilemma)")
        self.assertEqual(quote_differences[0]["body_term"], "트리핀 딜레마(Triffin’s Dilemma)")

    def test_three_term_option_retains_previous_records_exactly(self):
        selected = extract_terms(targets=TARGETS)
        by_title = {term.term: term for term in self.terms}
        self.assertEqual([term.term for term in selected], list(TARGETS))
        previous = json.loads(LEGACY_DIR.joinpath("terms_raw.json").read_text())
        self.assertEqual([term.to_dict() for term in selected], previous)
        self.assertEqual(selected, [by_title[title] for title in TARGETS])

    def test_no_empty_duplicate_or_outside_body_records(self):
        self.assertEqual(self.report["empty_definitions"], [])
        self.assertEqual(self.report["duplicate_terms"], [])
        self.assertEqual(self.report["duplicate_toc_entries"], [])
        for term in self.terms:
            self.assertTrue(term.definition.strip())
            self.assertTrue(19 <= term.source_pdf_page <= 423)
            self.assertEqual(term.source_book_page, term.source_pdf_page - 18)
        self.assertEqual(self.terms[0].term, "가계부실위험지수(HDRI)")
        self.assertEqual(self.terms[-1].term, "V-KOSPI")
        self.assertNotIn("발행인", self.terms[-1].definition)

    def test_characters_preserved_for_all_definitions(self):
        for term in self.terms:
            with self.subTest(term=term.term):
                self.assertEqual(re.sub(r"\s", "", term.definition),
                                 re.sub(r"\s", "", clean_definition(term.definition)))

    def test_page_continuation_beyond_stock_terms(self):
        household = next(term for term in self.terms if term.term == "가계수지")
        self.assertIn("흑자를 \n냈다면", household.definition)
        self.assertTrue(household.definition.rstrip().endswith("항목이 있다."))
        self.assertEqual(household.related_terms, ["경상수지", "재정수지"])

    def test_formula_and_table_candidates_not_removed(self):
        reviews = {entry["term"]: entry for entry in self.report["manual_review"]}
        self.assertIn("formula_symbols", reviews["가계순저축률"]["reasons"])
        self.assertIn("formula_font", reviews["금리평가이론"]["reasons"])
        self.assertIn("table_layout", reviews["죄수의 딜레마"]["reasons"])
        formula = next(term for term in self.terms if term.term == "가계순저축률")
        self.assertIn("× 100", formula.definition)
        self.assertIn("(-1, -1)", next(t for t in self.terms if t.term == "죄수의 딜레마").definition)

    def test_short_definitions_flagged_without_rewriting(self):
        flagged = {entry["term"] for entry in self.report["unusual_lengths"]}
        self.assertEqual(flagged, {"만기수익률", "모기지대출", "빈일자리율"})
        self.assertIn("만기수익률", {term.term for term in self.terms})

    def test_reference_audit_and_unresolved_differences_are_visible(self):
        self.assertTrue(self.report["checks"]["independent_reference_no_missing_characters"])
        self.assertTrue(self.report["checks"]["no_obvious_body_contamination"])
        self.assertFalse(self.report["checks"]["independent_reference_text_match"])
        differences = self.report["reference_text_differences"]
        self.assertEqual(len(differences), 11)
        self.assertEqual(sum(entry["order_only"] for entry in differences), 9)
        controls = [entry for entry in self.report["manual_review"]
                    if "pdf_control_character" in entry["reasons"]]
        self.assertEqual(len(controls), 2)
        self.assertEqual(len(self.report["manual_review"]), self.report["summary"]["review_term_count"])

    def test_json_files_roundtrip_with_original_and_cleaned_content(self):
        with tempfile.TemporaryDirectory() as directory:
            write_results(directory, self.terms, self.report)
            raw = json.loads((Path(directory) / "all_terms_raw.json").read_text())
            cleaned = json.loads((Path(directory) / "all_terms_cleaned.json").read_text())
            self.assertEqual(raw, [term.to_dict() for term in self.terms])
            self.assertEqual(cleaned, [term.to_dict(True) for term in self.terms])
            self.assertTrue((Path(directory) / "all_terms_report.md").exists())

    def test_three_term_cli_prints_valid_json_and_saves_files(self):
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run(
                [sys.executable, str(PROJECT_ROOT / "scripts" / "pdf_terms.py"),
                 "--scope", "three", "--output-dir", directory],
                capture_output=True, text=True, check=True,
            )
            records = json.loads(result.stdout)
            self.assertEqual([record["term"] for record in records], list(TARGETS))
            saved = json.loads((Path(directory) / "glossary.json").read_text())
            self.assertEqual(records, saved)

    def test_validation_detects_missing_duplicates_empty_and_mixed_content(self):
        selected = [term for term in self.terms if term.term in TARGETS]
        damaged = [replace(selected[0], definition=""),
                   replace(selected[0], definition="연관검색어 잘못 섞인 본문"),
                   replace(selected[1], source_book_page=999)]
        report = validate_extraction(DEFAULT_PDF, damaged)
        self.assertFalse(report["checks"]["toc_title_and_page_match"])
        self.assertFalse(report["checks"]["no_duplicate_terms"])
        self.assertFalse(report["checks"]["no_empty_definitions"])
        self.assertFalse(report["checks"]["page_mapping"])
        self.assertFalse(report["checks"]["no_obvious_body_contamination"])
        self.assertFalse(report["checks"]["independent_reference_no_missing_characters"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
