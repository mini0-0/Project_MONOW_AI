from collections import Counter
from copy import deepcopy
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest

from pdf.glossary import (
    FORMULA_FIELDS,
    GLOSSARY_FIELDS,
    build_glossary,
    classify_review,
    prepare_definition,
    write_outputs,
)
from pdf.paths import DEBUG_DIR, DEFAULT_PDF, LEGACY_DIR, PROJECT_ROOT, REVIEW_DECISIONS
from pdf.parser import REGIONS
from pdf.formula_separator import FORMULA_PATTERNS
from pdf.definition_corrections import correct_definition
from pdf.extractor import TARGETS


class GlossaryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.glossary, cls.report, cls.debug = build_glossary()
        cls.by_title = {record["term"]: record for record in cls.glossary}

    def test_export_789_terms_with_service_schema(self):
        self.assertEqual(len(self.glossary), 789)
        self.assertTrue(all(set(record) == GLOSSARY_FIELDS for record in self.glossary))

        for record in self.glossary:
            for formula in record["formulas"]:
                self.assertEqual(set(formula), set(FORMULA_FIELDS))

        serialized = json.dumps(self.glossary)

        for key in ("ast", "mathml", "source_indices", "source_bbox", "source_text"):
            self.assertNotIn(f'"{key}"', serialized)

    def test_preserve_original_definitions_in_export(self):
        baseline = json.loads((LEGACY_DIR / "all_terms_raw.json").read_text())
        self.assertEqual(self.report["original_records"], baseline)
        self.assertTrue(self.report["checks"]["original_records_match_baseline"])

        for record in self.glossary:
            self.assertNotIn("\n", record["definition"])
            self.assertNotIn("\r", record["definition"])

    def test_preserve_characters_outside_verified_corrections(self):
        # Given
        compact = lambda text: re.sub(r"\s", "", text)
        decisions = json.loads(REVIEW_DECISIONS.read_text())
        raw = {item["term"]: item for item in self.report["original_records"]}

        for record, enriched in zip(self.glossary, self.debug["enriched"]):
            # When
            expected, _ = correct_definition(
                raw[record["term"]],
                prepare_definition(enriched["definition_with_formulas"]),
                decisions,
            )

            # Then
            self.assertEqual(record["definition"], expected)
        self.assertTrue(self.report["checks"]["outside_formula_characters_preserved"])

    def test_export_formula_counts_and_source_pages(self):
        restored = [record for record in self.glossary if record["formulas"]]
        self.assertEqual(
            {record["term"] for record in restored}, set(REGIONS) | set(FORMULA_PATTERNS)
        )
        self.assertEqual(sum(len(record["formulas"]) for record in restored), 59)
        self.assertEqual(self.report["summary"]["restored_term_count"], 8)
        self.assertEqual(self.report["summary"]["equation_count"], 9)
        for original in self.debug["enriched"]:
            if original["term"] in REGIONS:
                self.assertEqual(
                    [item["expression"] for item in self.by_title[original["term"]]["formulas"]],
                    [item["text"] for item in original["formulas"]],
                )
        unit = self.by_title["단위노동비용"]
        self.assertEqual(unit["source_pdf_page"], 107)
        internal = next(item for item in self.debug["enriched"] if item["term"] == "단위노동비용")
        self.assertEqual(internal["formulas"][0]["source_pdf_page"], 108)
        self.assertEqual(internal["formulas"][0]["source_book_page"], 90)
        self.assertEqual(self.by_title[TARGETS[0]]["formulas"], [])

    def test_restore_taylor_pi_and_keep_formula_position(self):
        growth = self.by_title["경제성장률"]
        definition = growth["definition"]
        equation = growth["formulas"][0]["expression"]
        self.assertLess(definition.index("의미한다."), definition.index(equation))
        self.assertLess(definition.index(equation), definition.index("한편 수 개년"))
        self.assertNotIn("100", equation)
        taylor = self.by_title["테일러 준칙(Taylor’s Rule)"]
        self.assertEqual(taylor["definition"].count("π"), 5)
        self.assertNotIn("\uf0bc", taylor["definition"])
        self.assertIn("π_t^*: 물가목표", taylor["definition"])

    def test_distinguish_verified_and_pending_reviews(self):
        self.assertEqual(
            Counter(record["review_status"] for record in self.glossary),
            {"verified": 83, "automated_checks_passed": 704, "needs_review": 2},
        )
        for term in ("서킷브레이커", "죄수의 딜레마", "BIS 자기자본비율", "한계비용", "IFRS17"):
            self.assertEqual(self.by_title[term]["review_status"], "verified")
        for term in REGIONS:
            self.assertEqual(self.by_title[term]["review_status"], "verified")
        self.assertEqual(self.report["summary"]["unresolved_error_term_count"], 0)
        self.assertEqual(self.by_title[TARGETS[0]]["review_status"], "automated_checks_passed")

    def test_remove_hidden_characters_without_changing_raw_text(self):
        self.assertNotIn("\xad", self.by_title["한계비용"]["definition"])
        self.assertNotIn("\u200b", self.by_title["IFRS17"]["definition"])
        raw = next(
            record for record in self.report["original_records"] if record["term"] == "한계비용"
        )
        self.assertIn("\xad", raw["definition"])

    def test_restore_verified_table_cell_relations(self):
        circuit = self.by_title["서킷브레이커"]["definition"]
        self.assertIn("1단계 | 전일 대비 8% 이상 하락 | 20분간 매매거래중단", circuit)
        self.assertIn(
            "2단계2) | 전일 대비 15% 이상 하락, 1단계 서킷브레이커 발동시점 대비 1% 이상 추가 하락 | 20분간 매매거래중단",
            circuit,
        )
        self.assertIn(
            "3단계2) | 전일 대비 20% 이상 하락, 2단계 서킷브레이커 발동시점 대비 1% 이상 추가 하락 | 당일 장종료 조치",
            circuit,
        )
        matrix = self.by_title["죄수의 딜레마"]["definition"]
        self.assertIn(
            "용의자 Ⅰ 자백함 | 용의자 Ⅱ 자백함: (-1, -1) | 용의자 Ⅱ 자백하지 않음: (1, -2)", matrix
        )
        self.assertIn(
            "용의자 Ⅰ 자백하지 않음 | 용의자 Ⅱ 자백함: (-2, 1) | 용의자 Ⅱ 자백하지 않음: (0, 0)",
            matrix,
        )
        bis = self.by_title["BIS 자기자본비율"]["definition"]

        for expected in (
            "보통주자본(CET 1): 4.5%",
            "보통주자본(CET 1)+AT 1: 6%",
            "보완자본(Tier 2): 8.0%",
            "최소비율+CCoB+CCyB+SIB: 9.0%",
            "기타 기본자본(AT 1): 10.5%",
            "보완자본(Tier 2): 12.5%",
        ):
            self.assertIn(expected, bis)

    def test_restore_annotations_and_preserve_pending_reviews(self):
        self.assertIn(
            "순생산액(순소득) = 총생산액(총소득)(= 산출액 - 중간투입액) - 고정자본소모",
            self.by_title["고정자본소모"]["definition"],
        )
        self.assertIn("10,080원 − 10,000원 = 80원", self.by_title["한계비용"]["definition"])
        lorenz = self.by_title["로렌츠곡선"]
        self.assertIn(
            "가로축 인구의 누적비율(%), 0~100; 세로축 소득 누적점유율(%), 0~100",
            lorenz["definition"],
        )
        self.assertNotIn("대각선득", lorenz["definition"])
        self.assertEqual(lorenz["review_status"], "needs_review")

        for record in self.glossary:
            self.assertFalse(
                any(
                    c in record["definition"]
                    for c in ("\u200b", "\u200c", "\x07", "\xad", "\uf0bc")
                )
            )
        savings = self.by_title["총저축/총저축률/평균소비성향/평균저축성향"]
        self.assertIn("고정자본소모)", savings["definition"])
        self.assertEqual(savings["review_status"], "needs_review")

    def test_reject_changed_or_partial_definition_corrections(self):
        # Given
        corrections = self.report["service_definition_corrections"]
        self.assertEqual(len(corrections), 9)
        self.assertEqual(sum(c["status"] == "verified" for c in corrections), 7)
        raw = {item["term"]: item for item in self.report["original_records"]}
        enriched = {item["term"]: item for item in self.debug["enriched"]}
        decisions = json.loads(REVIEW_DECISIONS.read_text())
        for item in corrections:
            title = item["term"]
            text = prepare_definition(enriched[title]["definition_with_formulas"])
            for change in item["changes"]:
                self.assertEqual(text.count(change["before"]), 1)
                text = text.replace(change["before"], change["after"], 1)
            self.assertEqual(text, self.by_title[title]["definition"])
            altered = dict(raw[title], definition=raw[title]["definition"] + "changed")

            # When
            unchanged, result = correct_definition(altered, text, decisions)

            # Then
            self.assertEqual(unchanged, text)
            self.assertFalse(result["applied"])

        # Given
        original = prepare_definition(
            raw["국민처분가능소득(NDI)/국민총처분가능소득(GNDI)"]["definition"]
        )
        altered = original.replace("뺀 금액국민총처분가능소득(GNDI)", "changed")

        # When
        text, result = correct_definition(
            raw["국민처분가능소득(NDI)/국민총처분가능소득(GNDI)"], altered, decisions
        )

        # Then
        self.assertEqual(text, altered)  # 첫 수정 성공 후 두 번째 실패해도 부분 적용하지 않는다.
        self.assertFalse(result["applied"])

    def test_write_only_two_files_without_debug_output(self):
        # Given
        existing_debug = {str(path) for path in DEBUG_DIR.rglob("*") if path.is_file()}
        with tempfile.TemporaryDirectory() as directory:
            out, debug = Path(directory) / "out", Path(directory) / "debug"

            # When
            write_outputs(out, self.glossary, self.report, debug_dir=debug)

            # Then
            self.assertEqual(
                {path.name for path in out.iterdir()}, {"glossary.json", "validation_report.json"}
            )
            self.assertFalse(debug.exists())
            self.assertEqual(json.loads((out / "glossary.json").read_text()), self.glossary)
        self.assertEqual(
            existing_debug, {str(path) for path in DEBUG_DIR.rglob("*") if path.is_file()}
        )

    def test_write_debug_details_outside_service_output(self):
        # Given
        with tempfile.TemporaryDirectory() as directory:
            out, debug = Path(directory) / "out", Path(directory) / "debug"

            # When
            write_outputs(out, self.glossary, self.report, debug_data=self.debug, debug_dir=debug)

            # Then
            self.assertEqual(
                {p.name for p in out.iterdir()}, {"glossary.json", "validation_report.json"}
            )
            detailed = json.loads((debug / "all_terms_with_formulas.json").read_text())
            formula = next(record for record in detailed if record["formulas"])["formulas"][0]
            self.assertIn("ast", formula)
            self.assertIn("source_indices", formula)
            self.assertTrue((debug / "all_terms_formula_comparison.html").exists())
            self.assertEqual(len(list((debug / "all_terms_formula_evidence").glob("*.png"))), 11)

    def test_reject_debug_directory_inside_output(self):
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / "out"
            for debug in (out, out / "debug"):
                with self.assertRaisesRegex(ValueError, "밖이어야"):
                    write_outputs(
                        out, self.glossary, self.report, debug_data=self.debug, debug_dir=debug
                    )
            self.assertFalse(out.exists())

    def test_export_selected_term_from_another_working_directory(self):
        # Given
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / "out"

            # When
            result = subprocess.run(
                [
                    sys.executable,
                    str(PROJECT_ROOT / "pdf" / "extractor.py"),
                    "--terms",
                    "PBR",
                    "--output-dir",
                    str(out),
                ],
                cwd=directory,
                capture_output=True,
                text=True,
                check=True,
            )
            records = json.loads(result.stdout)

            # Then
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["term"], TARGETS[1])
            self.assertEqual(
                (records[0]["source_pdf_page"], records[0]["source_book_page"]), (310, 292)
            )
            self.assertEqual(json.loads((out / "glossary.json").read_text()), records)

    def test_require_review_for_changed_raw_records(self):
        # Given
        record = deepcopy(
            next(record for record in self.debug["enriched"] if record["term"] == "경제성장률")
        )
        raw = next(
            record for record in self.report["original_records"] if record["term"] == "경제성장률"
        ).copy()
        raw["definition"] += "임의 변경"
        decisions = json.loads(REVIEW_DECISIONS.read_text())

        # When
        status, issues = classify_review(record, raw, decisions, {})

        # Then
        self.assertEqual(status, "needs_review")
        self.assertIn("baseline_record_mismatch", [issue["code"] for issue in issues])


if __name__ == "__main__":
    unittest.main(verbosity=2)
