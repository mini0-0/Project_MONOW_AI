from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, redirect_stdout
from copy import deepcopy
import io
import json
from threading import Event
import unittest
from unittest.mock import MagicMock, Mock, patch

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
from scripts.generate_glossary_explanations import generate_for_term, main, prepare_table
from tests.db.test_glossary_repository import isolated_glossary_database


class ExplanationStorageTests(unittest.TestCase):
    def test_resume_after_quota_stop_without_regenerating_saved_terms(self):
        # Given
        terms = ["고용률", "주가수익비율(PER)", "주가순자산비율(PBR)"]
        first_client = Mock()
        first_client.generate.side_effect = [
            ("15세 이상 인구에서 취업자가 차지하는 비율입니다.", "mock-model"),
            QuotaExceededError("mock quota reached"),
        ]
        second_client = Mock()
        second_client.generate.return_value = ("초보자를 위한 저장된 설명입니다.", "mock-model")

        def run(client):
            output = io.StringIO()
            with (
                patch(
                    "scripts.generate_glossary_explanations.create_database_engine",
                    return_value=self.engine,
                ),
                patch(
                    "scripts.generate_glossary_explanations.prepare_table",
                    side_effect=lambda engine: prepare_table(engine, testing=True),
                ),
                patch(
                    "scripts.generate_glossary_explanations.generate_for_term",
                    side_effect=lambda engine, term, generate: generate_for_term(
                        engine, term, generate, testing=True
                    ),
                ),
                patch(
                    "scripts.generate_glossary_explanations.GeminiClient.from_env",
                    return_value=client,
                ) as factory,
                redirect_stdout(output),
            ):
                code = main(["--terms", *terms])
            return code, json.loads(output.getvalue()), factory

        # When
        code, report, _ = run(first_client)

        # Then
        self.assertEqual(code, 1)
        self.assertEqual([r["status"] for r in report["results"]], ["created", "stopped"])
        self.assertEqual(first_client.generate.call_count, 2)
        first_client.close.assert_called_once()
        before = self.stored()
        self.assertEqual(len(before), 1)

        # When
        code, report, _ = run(second_client)

        # Then
        self.assertEqual(code, 0)
        self.assertEqual(
            [r["status"] for r in report["results"]], ["skipped_existing", "created", "created"]
        )
        self.assertEqual(second_client.generate.call_count, 2)
        self.assertEqual(len(self.stored()), 3)
        self.assertEqual(next(r for r in self.stored() if r["term"] == "고용률"), before[0])

        # Given
        third_client = Mock()

        # When
        code, report, factory = run(third_client)

        # Then
        self.assertEqual(code, 0)
        self.assertEqual(report["counts"], {"skipped_existing": 3})
        factory.assert_not_called()
        third_client.generate.assert_not_called()
        self.assert_source_unchanged()

    def test_match_sql_and_python_character_counts(self):
        values = (
            "가， " * 59 + "가.",
            "가" * 61 + ".",
            "가，、。‘’\u00a0나\u202f+₩",
            "금리가 0.5% 변하는 비율입니다.",
        )

        with self.engine.connect() as connection:
            for value in values:
                with self.subTest(value=value):
                    sql_count = connection.scalar(
                        select(
                            func.char_length(
                                func.regexp_replace(value, SIMPLE_IGNORED_PATTERN, "", "g")
                            )
                        )
                    )
                    self.assertEqual(sql_count, simple_explanation_length(value))

    def test_save_sixty_characters_excluding_punctuation(self):
        value = "가， " * 59 + "가."
        self.client.generate.return_value = (value, "mock-model")
        result = generate_for_term(self.engine, "고용률", self.generate, testing=True)
        self.assertEqual(result["status"], "created")
        self.assertEqual(self.stored()[0]["explanation"], value)

    def test_reject_invalid_sentence_format_in_database(self):
        generate_for_term(self.engine, "고용률", self.generate, testing=True)
        candidate = {
            key: value
            for key, value in self.stored()[0].items()
            if key not in {"created_at", "id", "term"}
        }

        candidate["term_id"] = self.source_id("주가수익비율(PER)")

        for text in ("설명입니다?", "설명입니다!", "첫째입니다. 둘째입니다."):
            candidate["explanation"] = text
            with self.subTest(text=text), self.assertRaises(IntegrityError):
                with self.engine.begin() as connection:
                    GlossaryExplanationRepository(connection).save_if_absent(candidate)
        self.assertEqual(len(self.stored()), 1)

    def test_reject_legacy_constraint_without_changing_rows(self):
        # Given
        generate_for_term(self.engine, "고용률", self.generate, testing=True)
        schema = self.engine.get_execution_options()["schema_translate_map"]["public"]
        quoted = self.engine.dialect.identifier_preparer.quote(schema)
        table = f"{quoted}.glossary_explanations"

        with self.engine.begin() as connection:
            connection.exec_driver_sql(
                f"ALTER TABLE {table} DROP CONSTRAINT {SIMPLE_CONSTRAINT_NAME}"
            )
            connection.exec_driver_sql(
                f"ALTER TABLE {table} ADD CONSTRAINT "
                "simple_explanation_format "
                "CHECK (char_length(explanation) <= 100)"
            )
            connection.execute(
                GlossaryExplanation.__table__.update().values(explanation="가" * 61 + ".")
            )
        before = deepcopy(self.stored())
        self.client.reset_mock()

        # When
        with self.assertRaisesRegex(ValueError, "수동 스키마 변경"):
            prepare_table(self.engine, testing=True)
        result = generate_for_term(self.engine, "고용률", self.generate, testing=True)

        # Then
        self.assertEqual(result["status"], "skipped_invalid")
        self.client.generate.assert_not_called()
        self.assertEqual(self.stored(), before)
        self.assert_source_unchanged()

    def setUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        self.engine, self.source_records = stack.enter_context(isolated_glossary_database())
        prepare_table(self.engine, testing=True)
        self.client = Mock()
        self.client.generate.return_value = (
            "15세 이상 인구에서 취업자가 차지하는 비율입니다.",
            "mock-model",
        )

    def generate(self, term, definition):
        return generate_explanation(self.client, term, definition)

    def source_id(self, term):
        with self.engine.connect() as connection:
            return connection.scalar(select(GlossaryTerm.id).where(GlossaryTerm.term == term))

    def stored(self):
        with self.engine.connect() as connection:
            return [
                dict(row)
                for row in connection.execute(
                    select(GlossaryExplanation.__table__, GlossaryTerm.term).join(
                        GlossaryTerm, GlossaryExplanation.term_id == GlossaryTerm.id
                    )
                ).mappings()
            ]

    def assert_source_unchanged(self):
        with self.engine.connect() as connection:
            actual = [
                dict(row) for row in connection.execute(select(GlossaryTerm.__table__)).mappings()
            ]
        self.assertEqual(
            {r["term"]: {key: value for key, value in r.items() if key != "id"} for r in actual},
            {r["term"]: r for r in self.source_records},
        )

    def test_skip_llm_for_existing_saved_explanation(self):
        result = generate_for_term(self.engine, "고용률", self.generate, testing=True)
        self.assertEqual(result["status"], "created")
        result = generate_for_term(self.engine, "고용률", self.generate, testing=True)
        self.assertEqual(result["status"], "skipped_existing")
        self.client.generate.assert_called_once()
        rows = self.stored()
        self.assertEqual(len(rows), 1)
        source = next(r for r in self.source_records if r["term"] == "고용률")
        self.assertEqual(rows[0]["definition_hash"], definition_hash(source["definition"]))
        self.assertEqual(rows[0]["model"], "mock-model")
        self.assertEqual(rows[0]["prompt_version"], "simple-v3-60")
        self.assertIsNotNone(rows[0]["created_at"].tzinfo)
        self.assert_source_unchanged()

    def test_skip_llm_for_review_required_or_unknown_term(self):
        for term, status in (("로렌츠곡선", "skipped_needs_review"), ("없는 용어", "not_found")):
            self.assertEqual(
                generate_for_term(self.engine, term, self.generate, testing=True)["status"], status
            )
        self.client.generate.assert_not_called()
        self.assertEqual(self.stored(), [])

    def test_preserve_database_after_invalid_llm_output(self):
        for value in ("", "가" * 61 + ".", "첫 문장입니다. 둘째입니다.", "설명\n입니다."):
            self.client.generate.return_value = (value, "mock-model")
            with self.subTest(value=value), self.assertRaises(GenerationError):
                generate_for_term(self.engine, "고용률", self.generate, testing=True)
            self.assertEqual(self.stored(), [])
        self.client.generate.side_effect = GenerationError("mock timeout")

        with self.assertRaises(GenerationError):
            generate_for_term(self.engine, "고용률", self.generate, testing=True)
        self.assertEqual(self.stored(), [])
        self.assert_source_unchanged()

    def test_rollback_database_failure_and_allow_retry(self):
        def invalid_metadata(term, definition):
            return {**self.generate(term, definition), "model": ""}

        with self.assertRaises(IntegrityError):
            generate_for_term(self.engine, "고용률", invalid_metadata, testing=True)
        self.assertEqual(self.stored(), [])
        self.assertEqual(
            generate_for_term(self.engine, "고용률", self.generate, testing=True)["status"],
            "created",
        )
        self.assert_source_unchanged()

    def test_keep_unique_explanations_for_each_type(self):
        # Given
        generate_for_term(self.engine, "고용률", self.generate, testing=True)
        before = self.stored()[0]

        with self.engine.begin() as connection:
            repository = GlossaryExplanationRepository(connection)
            candidate = {k: v for k, v in before.items() if k not in {"created_at", "id", "term"}}
            candidate["explanation"] = "덮어쓰면 안 되는 설명입니다."

            # When
            self.assertFalse(repository.save_if_absent(candidate))
            candidate["explanation_type"] = "DETAILED"
            candidate["explanation"] = "상세 설명 " * 30
            self.assertTrue(repository.save_if_absent(candidate))

            # Then
            self.assertEqual(repository.find("고용률")["explanation"], before["explanation"])
            self.assertEqual(
                repository.find("고용률", "DETAILED")["explanation"], candidate["explanation"]
            )

        self.assertEqual(len(self.stored()), 2)

    def test_reject_oversized_simple_explanation_in_database(self):
        generate_for_term(self.engine, "고용률", self.generate, testing=True)
        candidate = {
            k: v for k, v in self.stored()[0].items() if k not in {"created_at", "id", "term"}
        }
        candidate["term_id"] = self.source_id("주가수익비율(PER)")
        candidate["explanation"] = "가" * 61 + "."

        with self.assertRaises(IntegrityError), self.engine.begin() as connection:
            GlossaryExplanationRepository(connection).save_if_absent(candidate)
        self.assertEqual(len(self.stored()), 1)

    def test_preserve_stale_explanation_without_overwriting(self):
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
            generate_for_term(self.engine, "고용률", self.generate, testing=True)["status"],
            "skipped_stale",
        )
        self.client.generate.assert_not_called()
        self.assertEqual(self.stored(), before)
        self.assert_source_unchanged()

    def test_generate_once_for_concurrent_requests(self):
        # Given
        started, release = Event(), Event()

        def blocked(term, definition):
            started.set()
            if not release.wait(10):
                raise GenerationError("test timeout")
            return self.generate(term, definition)

        # When
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(generate_for_term, self.engine, "고용률", blocked, testing=True)
            try:
                self.assertTrue(started.wait(5))
                second = generate_for_term(self.engine, "고용률", self.generate, testing=True)
                self.assertEqual(second["status"], "skipped_in_progress")
            finally:
                release.set()
            self.assertEqual(future.result(timeout=5)["status"], "created")

        # Then
        self.client.generate.assert_called_once()
        self.assertEqual(len(self.stored()), 1)
        self.assert_source_unchanged()


class MissingExplanationTests(unittest.TestCase):
    def run_main(self, arguments, coverage, outcomes=()):
        output = io.StringIO()
        engine = MagicMock()
        stack = ExitStack()

        with stack:
            stack.enter_context(
                patch(
                    "scripts.generate_glossary_explanations.create_database_engine",
                    return_value=engine,
                )
            )
            prepare = stack.enter_context(
                patch("scripts.generate_glossary_explanations.prepare_table")
            )
            stack.enter_context(patch("scripts.generate_glossary_explanations._check_database"))
            repository = stack.enter_context(
                patch("scripts.generate_glossary_explanations." "GlossaryExplanationRepository")
            )
            repository.return_value.missing_terms.return_value = ["국채", "회사채"]
            stack.enter_context(
                patch(
                    "scripts.generate_glossary_explanations.check_coverage", return_value=coverage
                )
            )
            generate = stack.enter_context(
                patch(
                    "scripts.generate_glossary_explanations.generate_for_term", side_effect=outcomes
                )
            )
            client = stack.enter_context(
                patch("scripts.generate_glossary_explanations.GeminiClient.from_env")
            )
            stack.enter_context(redirect_stdout(output))
            code = main(arguments)

        return code, json.loads(output.getvalue()), generate, client, prepare

    def test_process_only_missing_terms_in_all_mode(self):
        coverage = {"complete": True, "total_terms": 789, "missing_count": 0}
        code, report, generate, client, _ = self.run_main(
            ["--all"],
            coverage,
            [{"term": "국채", "status": "created"}, {"term": "회사채", "status": "created"}],
        )
        self.assertEqual(code, 0)
        self.assertEqual([c.args[1] for c in generate.call_args_list], ["국채", "회사채"])
        self.assertEqual(report["coverage"], coverage)

    def test_check_coverage_without_generation_or_schema_changes(self):
        coverage = {"complete": False, "total_terms": 789, "missing_count": 62}
        code, report, generate, client, prepare = self.run_main(["--check-only"], coverage)
        self.assertEqual(code, 1)
        self.assertEqual(report["coverage"]["missing_count"], 62)
        generate.assert_not_called()
        client.assert_not_called()
        prepare.assert_not_called()

    def test_log_llm_stop_and_skip_remaining_terms(self):
        # Given
        error = QuotaExceededError("safe error", http_status=429)

        # When
        with self.assertLogs("scripts.generate_glossary_explanations", level="ERROR") as logs:
            code, report, generate, _, _ = self.run_main(["--all"], {"complete": False}, [error])

        # Then
        self.assertEqual(code, 1)
        self.assertEqual(generate.call_count, 1)
        self.assertEqual(report["results"][0]["http_status"], 429)
        self.assertIn("llm_stopped", logs.output[0])

    def test_log_database_failure_safely_and_continue(self):
        # Given
        error = IntegrityError("secret_sql", {"password": "secret_value"}, Exception())

        # When
        with self.assertLogs("scripts.generate_glossary_explanations", level="ERROR") as logs:
            code, report, generate, _, _ = self.run_main(
                ["--all"], {"complete": False}, [error, {"term": "회사채", "status": "created"}]
            )

        # Then
        self.assertEqual(code, 1)
        self.assertEqual(generate.call_count, 2)
        self.assertEqual(report["results"][0]["error_type"], "DB_SAVE_ERROR")
        self.assertNotIn("secret_", " ".join(logs.output) + json.dumps(report))

    def test_continue_after_explanation_validation_failure(self):
        # Given
        with self.assertLogs("scripts.generate_glossary_explanations", level="ERROR"):
            # When
            code, report, generate, _, _ = self.run_main(
                ["--all"],
                {"complete": False},
                [GenerationError("invalid explanation"), {"term": "회사채", "status": "created"}],
            )

        # Then
        self.assertEqual(code, 1)
        self.assertEqual(generate.call_count, 2)
        self.assertEqual(report["counts"], {"failed": 1, "created": 1})

    def test_filter_missing_query_by_simple_type_and_review_status(self):
        from sqlalchemy.dialects import postgresql

        connection = Mock()
        connection.scalars.return_value = ["국채"]
        repository = GlossaryExplanationRepository(connection)
        self.assertEqual(repository.missing_terms(), ["국채"])
        statement = connection.scalars.call_args.args[0]
        sql = str(statement.compile(dialect=postgresql.dialect()))
        self.assertIn("NOT (EXISTS", sql)
        self.assertIn("SIMPLE", statement.compile().params.values())
        self.assertIn("needs_review", statement.compile().params.values())

    def test_report_missing_and_review_blocked_terms(self):
        connection = Mock()
        result = connection.execute.return_value.mappings.return_value
        result.all.return_value = [
            {"term": "국채", "review_status": "verified", "saved_id": 11},
            {"term": "회사채", "review_status": "verified", "saved_id": None},
            {"term": "로렌츠곡선", "review_status": "needs_review", "saved_id": None},
        ]
        report = GlossaryExplanationRepository(connection).coverage()
        self.assertEqual(report["total_terms"], 3)
        self.assertEqual(report["simple_explanations"], 1)
        self.assertEqual(report["missing_count"], 2)
        self.assertEqual(report["blocked_needs_review"], ["로렌츠곡선"])
        self.assertFalse(report["complete"])

    def test_report_62_missing_explanations_out_of_789_terms(self):
        connection = Mock()
        rows = [
            {
                "term": f"term_{index}",
                "review_status": "verified",
                "saved_id": index + 1000 if index < 727 else None,
            }
            for index in range(789)
        ]
        result = connection.execute.return_value.mappings.return_value
        result.all.return_value = rows
        report = GlossaryExplanationRepository(connection).coverage()
        self.assertEqual(report["total_terms"], 789)
        self.assertEqual(report["simple_explanations"], 727)
        self.assertEqual(report["missing_count"], 62)
        self.assertEqual(len(report["missing_terms"]), 62)

    def test_print_migration_sql_without_database_or_llm_access(self):
        output = io.StringIO()
        with (
            patch(
                "scripts.generate_glossary_explanations.create_database_engine",
                side_effect=AssertionError("DB 접속 금지"),
            ),
            patch(
                "scripts.generate_glossary_explanations.GeminiClient.from_env",
                side_effect=AssertionError("LLM 호출 금지"),
            ),
            redirect_stdout(output),
        ):
            self.assertEqual(main(["--print-simple-constraint-sql"]), 0)
        sql = output.getvalue()
        self.assertIn("BEGIN;", sql)
        self.assertIn("COMMIT;", sql)
        self.assertIn("DROP CONSTRAINT IF EXISTS simple_explanation_format_v2", sql)
        self.assertIn("ADD CONSTRAINT simple_explanation_format_v3", sql)
        self.assertIn("BETWEEN 1 AND 60", sql)
        self.assertIn("regexp_replace", sql)
        self.assertNotIn("UPDATE ", sql)
        self.assertNotIn("DELETE ", sql)

    def test_preserve_old_prompt_result_without_regeneration(self):
        # Given
        engine = MagicMock()
        generate = Mock(side_effect=AssertionError("기존 설명 재생성 금지"))
        existing = {
            "explanation": "기존에 저장한 설명입니다.",
            "definition_hash": definition_hash("원본 설명"),
            "prompt_version": "simple-v2-30",
        }

        with (
            patch("scripts.generate_glossary_explanations._check_database"),
            patch(
                "scripts.generate_glossary_explanations." "GlossaryExplanationRepository"
            ) as factory,
        ):
            repository = factory.return_value
            repository.source_for_generation.return_value = {
                "definition": "원본 설명",
                "review_status": "verified",
            }
            repository.find.return_value = existing
            # When
            result = generate_for_term(engine, "국채", generate)

        # Then
        self.assertEqual(result["status"], "skipped_existing")
        generate.assert_not_called()
        repository.save_if_absent.assert_not_called()
        self.assertEqual(existing["prompt_version"], "simple-v2-30")


class IdGenerationTests(unittest.TestCase):
    def setUp(self):
        self.engine = MagicMock()
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch("scripts.generate_glossary_explanations._check_database"))
        factory = stack.enter_context(
            patch("scripts.generate_glossary_explanations." "GlossaryExplanationRepository")
        )
        self.repository = factory.return_value
        self.repository.try_generation_lock.return_value = True
        self.repository.source_for_generation.return_value = {
            "id": 2**40,
            "term": "국채",
            "definition": "원본 정의",
            "review_status": "verified",
        }
        self.repository.find.return_value = None
        self.repository.save_if_absent.return_value = True
        self.generate = Mock(
            return_value={
                "explanation": "가" * 60 + ".",
                "model": "mock-model",
                "prompt_version": "simple-v3-60",
                "definition_hash": definition_hash("원본 정의"),
            }
        )

    def test_save_source_id_as_explanation_fk(self):
        result = generate_for_term(self.engine, "국채", self.generate)
        self.assertEqual(result["status"], "created")
        self.generate.assert_called_once_with("국채", "원본 정의")
        record = self.repository.save_if_absent.call_args.args[0]
        self.assertEqual(record["term_id"], 2**40)
        self.assertEqual(record["explanation_type"], "SIMPLE")
        self.assertNotIn("term", record)
        self.assertNotIn("id", record)

    def test_preserve_existing_explanation_id_and_content(self):
        existing = {
            "id": 123,
            "term_id": 2**40,
            "explanation": "기존 설명입니다.",
            "prompt_version": "simple-v2-30",
            "definition_hash": definition_hash("원본 정의"),
        }

        before = deepcopy(existing)
        self.repository.find.return_value = existing
        result = generate_for_term(self.engine, "국채", self.generate)
        self.assertEqual(result["status"], "skipped_existing")
        self.generate.assert_not_called()
        self.repository.save_if_absent.assert_not_called()
        self.assertEqual(existing, before)

    def test_reject_invalid_explanation_before_saving_fk_record(self):
        self.generate.return_value["explanation"] = "가" * 61 + "."
        with self.assertRaises(GenerationError):
            generate_for_term(self.engine, "국채", self.generate)
        self.repository.save_if_absent.assert_not_called()

    def test_keep_existing_row_after_concurrent_insert(self):
        self.repository.save_if_absent.return_value = False
        result = generate_for_term(self.engine, "국채", self.generate)
        self.assertEqual(result["status"], "skipped_existing")
