from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, redirect_stdout
from copy import deepcopy
import io
import json
from threading import Event
import unittest
from unittest.mock import Mock, patch

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from db.models import (
    SIMPLE_CONSTRAINT_NAME,
    SIMPLE_IGNORED_PATTERN,
    GlossaryExplanation,
    GlossaryTerm,
    simple_explanation_length,
)
from db.repositories.glossary_explanations import GlossaryExplanationRepository
from llm.client import GenerationError, QuotaExceededError
from llm.glossary_generator import definition_hash, generate_explanation
from scripts.generate_glossary_explanations import (
    generate_for_term,
    main,
    prepare_table,
)
from tests.db.test_glossary_repository import isolated_glossary_database


class ExplanationStorageTests(unittest.TestCase):
    def test_given_quota_stop_when_rerun_then_saved_terms_are_skipped(self):
        terms = ["고용률", "주가수익비율(PER)", "주가순자산비율(PBR)"]
        first_client = Mock()
        first_client.generate.side_effect = [
            ("15세 이상 인구에서 취업자가 차지하는 비율입니다.", "mock-model"),
            QuotaExceededError("mock quota reached"),
        ]
        second_client = Mock()
        second_client.generate.return_value = (
            "초보자를 위한 저장된 설명입니다.",
            "mock-model",
        )

        def run(client):
            output = io.StringIO()
            with patch(
                "scripts.generate_glossary_explanations.create_database_engine",
                return_value=self.engine,
            ), patch(
                "scripts.generate_glossary_explanations.prepare_table",
                side_effect=lambda engine: prepare_table(engine, testing=True),
            ), patch(
                "scripts.generate_glossary_explanations.generate_for_term",
                side_effect=lambda engine, term, generate: generate_for_term(
                    engine, term, generate, testing=True
                ),
            ), patch(
                "scripts.generate_glossary_explanations.GeminiClient.from_env",
                return_value=client,
            ) as factory, redirect_stdout(
                output
            ):
                code = main(["--terms", *terms])
            return code, json.loads(output.getvalue()), factory

        code, report, _ = run(first_client)
        self.assertEqual(code, 1)
        self.assertEqual(
            [r["status"] for r in report["results"]], ["created", "stopped"]
        )
        self.assertEqual(first_client.generate.call_count, 2)
        first_client.close.assert_called_once()
        before = self.stored()
        self.assertEqual(len(before), 1)

        code, report, _ = run(second_client)
        self.assertEqual(code, 0)
        self.assertEqual(
            [r["status"] for r in report["results"]],
            ["skipped_existing", "created", "created"],
        )
        self.assertEqual(second_client.generate.call_count, 2)
        self.assertEqual(len(self.stored()), 3)
        self.assertEqual(
            next(r for r in self.stored() if r["term"] == "고용률"), before[0]
        )

        third_client = Mock()
        code, report, factory = run(third_client)
        self.assertEqual(code, 0)
        self.assertEqual(report["counts"], {"skipped_existing": 3})
        factory.assert_not_called()
        third_client.generate.assert_not_called()
        self.assert_source_unchanged()

    def test_given_unicode_text_when_counted_then_sql_matches_python(self):
        values = (
            "가， " * 29 + "가.",
            "가" * 31 + ".",
            "가，、。‘’\u00a0나\u202f+₩",
            "금리가 0.5% 변하는 비율입니다.",
        )
        with self.engine.connect() as connection:
            for value in values:
                with self.subTest(value=value):
                    sql_count = connection.scalar(
                        select(
                            func.char_length(
                                func.regexp_replace(
                                    value, SIMPLE_IGNORED_PATTERN, "", "g"
                                )
                            )
                        )
                    )
                    self.assertEqual(
                        sql_count, simple_explanation_length(value)
                    )

    def test_given_ignored_punctuation_when_saved_then_thirty_is_accepted(
        self,
    ):
        value = "가， " * 29 + "가."
        self.client.generate.return_value = (value, "mock-model")
        result = generate_for_term(
            self.engine, "고용률", self.generate, testing=True
        )
        self.assertEqual(result["status"], "created")
        self.assertEqual(self.stored()[0]["explanation"], value)

    def test_given_nonfinal_period_when_saved_then_db_rejects(self):
        generate_for_term(self.engine, "고용률", self.generate, testing=True)
        candidate = {
            key: value
            for key, value in self.stored()[0].items()
            if key != "created_at"
        }
        candidate["term"] = "주가수익비율(PER)"
        for text in ("설명입니다?", "설명입니다!", "첫째입니다. 둘째입니다."):
            candidate["explanation"] = text
            with self.subTest(text=text), self.assertRaises(IntegrityError):
                with self.engine.begin() as connection:
                    GlossaryExplanationRepository(connection).save_if_absent(
                        candidate
                    )
        self.assertEqual(len(self.stored()), 1)

    def test_given_old_constraint_when_prepared_then_rows_are_preserved(self):
        generate_for_term(self.engine, "고용률", self.generate, testing=True)
        schema = self.engine.get_execution_options()["schema_translate_map"][
            "public"
        ]
        quoted = self.engine.dialect.identifier_preparer.quote(schema)
        table = f"{quoted}.glossary_explanations"
        with self.engine.begin() as connection:
            connection.exec_driver_sql(
                f"ALTER TABLE {table} DROP CONSTRAINT {SIMPLE_CONSTRAINT_NAME}"
            )
            connection.exec_driver_sql(
                f"ALTER TABLE {table} ADD CONSTRAINT "
                "simple_explanation_format "
                "CHECK (char_length(explanation) <= 60)"
            )
            connection.execute(
                GlossaryExplanation.__table__.update().values(
                    explanation="가" * 31 + "."
                )
            )
        before = deepcopy(self.stored())
        self.client.reset_mock()
        with self.assertRaisesRegex(ValueError, "수동 스키마 변경"):
            prepare_table(self.engine, testing=True)
        result = generate_for_term(
            self.engine, "고용률", self.generate, testing=True
        )
        self.assertEqual(result["status"], "skipped_invalid")
        self.client.generate.assert_not_called()
        self.assertEqual(self.stored(), before)
        self.assert_source_unchanged()

    def setUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        self.engine, self.source_records = stack.enter_context(
            isolated_glossary_database()
        )
        prepare_table(self.engine, testing=True)
        self.client = Mock()
        self.client.generate.return_value = (
            "15세 이상 인구에서 취업자가 차지하는 비율입니다.",
            "mock-model",
        )

    def generate(self, term, definition):
        return generate_explanation(self.client, term, definition)

    def stored(self):
        with self.engine.connect() as connection:
            return [
                dict(row)
                for row in connection.execute(
                    select(GlossaryExplanation.__table__)
                ).mappings()
            ]

    def assert_source_unchanged(self):
        with self.engine.connect() as connection:
            actual = [
                dict(row)
                for row in connection.execute(
                    select(GlossaryTerm.__table__)
                ).mappings()
            ]
        self.assertEqual(
            {r["term"]: r for r in actual},
            {r["term"]: r for r in self.source_records},
        )

    def test_given_saved_result_when_generated_again_then_llm_is_skipped(self):
        result = generate_for_term(
            self.engine, "고용률", self.generate, testing=True
        )
        self.assertEqual(result["status"], "created")
        result = generate_for_term(
            self.engine, "고용률", self.generate, testing=True
        )
        self.assertEqual(result["status"], "skipped_existing")
        self.client.generate.assert_called_once()
        rows = self.stored()
        self.assertEqual(len(rows), 1)
        source = next(r for r in self.source_records if r["term"] == "고용률")
        self.assertEqual(
            rows[0]["definition_hash"], definition_hash(source["definition"])
        )
        self.assertEqual(rows[0]["model"], "mock-model")
        self.assertEqual(rows[0]["prompt_version"], "simple-v2-30")
        self.assertIsNotNone(rows[0]["created_at"].tzinfo)
        self.assert_source_unchanged()

    def test_given_review_or_missing_term_when_generated_then_llm_is_skipped(
        self,
    ):
        for term, status in (
            ("로렌츠곡선", "skipped_needs_review"),
            ("없는 용어", "not_found"),
        ):
            self.assertEqual(
                generate_for_term(
                    self.engine, term, self.generate, testing=True
                )["status"],
                status,
            )
        self.client.generate.assert_not_called()
        self.assertEqual(self.stored(), [])

    def test_given_invalid_llm_output_when_generated_then_no_rows_are_saved(
        self,
    ):
        for value in (
            "",
            "가" * 31 + ".",
            "첫 문장입니다. 둘째입니다.",
            "설명\n입니다.",
        ):
            self.client.generate.return_value = (value, "mock-model")
            with self.subTest(value=value), self.assertRaises(GenerationError):
                generate_for_term(
                    self.engine, "고용률", self.generate, testing=True
                )
            self.assertEqual(self.stored(), [])
        self.client.generate.side_effect = GenerationError("mock timeout")
        with self.assertRaises(GenerationError):
            generate_for_term(
                self.engine, "고용률", self.generate, testing=True
            )
        self.assertEqual(self.stored(), [])
        self.assert_source_unchanged()

    def test_given_db_error_when_saved_then_rollback_allows_retry(self):
        def invalid_metadata(term, definition):
            return {**self.generate(term, definition), "model": ""}

        with self.assertRaises(IntegrityError):
            generate_for_term(
                self.engine, "고용률", invalid_metadata, testing=True
            )
        self.assertEqual(self.stored(), [])
        self.assertEqual(
            generate_for_term(
                self.engine, "고용률", self.generate, testing=True
            )["status"],
            "created",
        )
        self.assert_source_unchanged()

    def test_given_two_explanation_types_when_saved_then_uniqueness_is_kept(
        self,
    ):
        generate_for_term(self.engine, "고용률", self.generate, testing=True)
        before = self.stored()[0]
        with self.engine.begin() as connection:
            repository = GlossaryExplanationRepository(connection)
            candidate = {k: v for k, v in before.items() if k != "created_at"}
            candidate["explanation"] = "덮어쓰면 안 되는 설명입니다."
            self.assertFalse(repository.save_if_absent(candidate))
            candidate["explanation_type"] = "DETAILED"
            candidate["explanation"] = "상세 설명 " * 30
            self.assertTrue(repository.save_if_absent(candidate))
            self.assertEqual(
                repository.find("고용률")["explanation"], before["explanation"]
            )
            self.assertEqual(
                repository.find("고용률", "DETAILED")["explanation"],
                candidate["explanation"],
            )
        self.assertEqual(len(self.stored()), 2)

    def test_given_oversized_simple_when_inserted_then_db_rejects_row(self):
        generate_for_term(self.engine, "고용률", self.generate, testing=True)
        candidate = {
            k: v for k, v in self.stored()[0].items() if k != "created_at"
        }
        candidate["term"] = "주가수익비율(PER)"
        candidate["explanation"] = "가" * 31 + "."
        with self.assertRaises(
            IntegrityError
        ), self.engine.begin() as connection:
            GlossaryExplanationRepository(connection).save_if_absent(candidate)
        self.assertEqual(len(self.stored()), 1)

    def test_given_stale_result_when_generated_then_no_overwrite_occurs(self):
        generate_for_term(self.engine, "고용률", self.generate, testing=True)
        with self.engine.begin() as connection:
            connection.execute(
                GlossaryExplanation.__table__.update().values(
                    definition_hash=definition_hash("다른 원문")
                )
            )
        before = deepcopy(self.stored())
        self.client.reset_mock()
        self.assertEqual(
            generate_for_term(
                self.engine, "고용률", self.generate, testing=True
            )["status"],
            "skipped_stale",
        )
        self.client.generate.assert_not_called()
        self.assertEqual(self.stored(), before)
        self.assert_source_unchanged()

    def test_given_concurrent_requests_when_generated_then_llm_runs_once(self):
        started, release = Event(), Event()

        def blocked(term, definition):
            started.set()
            if not release.wait(10):
                raise GenerationError("test timeout")
            return self.generate(term, definition)

        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(
                generate_for_term, self.engine, "고용률", blocked, testing=True
            )
            try:
                self.assertTrue(started.wait(5))
                second = generate_for_term(
                    self.engine, "고용률", self.generate, testing=True
                )
                self.assertEqual(second["status"], "skipped_in_progress")
            finally:
                release.set()
            self.assertEqual(future.result(timeout=5)["status"], "created")
        self.client.generate.assert_called_once()
        self.assertEqual(len(self.stored()), 1)
        self.assert_source_unchanged()
