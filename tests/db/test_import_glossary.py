from contextlib import redirect_stderr
from copy import deepcopy
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from sqlalchemy import func, inspect, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.schema import CreateSchema, DropSchema

from db.database import create_database_engine, database_url, validate_database_name
from db.models import GlossaryTerm
from scripts.import_glossary import (
    DEFAULT_JSON,
    import_glossary,
    load_glossary,
    main,
    validate_records,
    verify_records,
)


class ImportValidationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.records = load_glossary()

    def test_validate_789_records_without_changing_fields(self):
        original = deepcopy(self.records)
        validate_records(self.records)
        self.assertEqual(len(self.records), 789)
        self.assertEqual(sum(len(r["formulas"]) for r in self.records), 59)
        self.assertEqual(self.records, original)

    def test_reject_wrong_record_count_and_duplicate_terms(self):
        with self.assertRaisesRegex(ValueError, "789"):
            validate_records(self.records[:-1])
        records = [self.records[0], self.records[0]]
        with self.assertRaisesRegex(ValueError, "중복 term"):
            validate_records(records, expected_count=2)

    def test_reject_invalid_record_fields(self):
        for field, value in (
            ("definition", " "),
            ("term", "a\x00b"),
            ("formulas", [{"expression": "x", "type": "formula"}]),
            ("formulas", [{"expression": ""}]),
            ("formulas", None),
            ("related_terms", [1]),
            ("source_pdf_page", True),
            ("source_pdf_page", 0),
            ("source_pdf_page", 2**31),
            ("source_book_page", -1),
            ("review_status", "unknown"),
        ):

            with self.subTest(field=field, value=value):
                record = dict(self.records[0], **{field: value})
                with self.assertRaises(ValueError):
                    validate_records([record], expected_count=1)

        for record in (
            {k: v for k, v in self.records[0].items() if k != "definition"},
            dict(self.records[0], unexpected="x"),
        ):
            with self.assertRaises(ValueError):
                validate_records([record], expected_count=1)

    def test_preserve_null_book_page(self):
        record = dict(self.records[0], source_book_page=None)
        validate_records([record], expected_count=1)
        self.assertIsNone(record["source_book_page"])

    def test_reject_duplicate_json_keys_and_invalid_json(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "input.json"
            for content in ('[{"term":"a","term":"b"}]', '{bad json'):
                path.write_text(content)
                with self.assertRaises(ValueError):
                    load_glossary(path)

    def test_apply_environment_overrides_and_preserve_password(self):
        # Given
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            env = Path(directory) / ".env"
            env.write_text(
                "DB_HOST=localhost\nDB_PORT=5432\nDB_NAME=monow_ai\n"
                "DB_USER=test_user\nDB_PASSWORD='p@ss:/#word'\n"
            )

            # When
            url = database_url(env)

            # Then
            self.assertEqual(url.password, "p@ss:/#word")
            self.assertNotIn(url.password, str(url))
            self.assertNotIn("DB_PASSWORD", os.environ)
            with patch.dict(os.environ, {"DB_HOST": "override"}):
                self.assertEqual(database_url(env).host, "override")

    def test_reject_missing_database_settings_and_invalid_port(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            env = Path(directory) / ".env"
            env.write_text("DB_NAME=monow_ai\n")
            with self.assertRaisesRegex(ValueError, "DB_USER"):
                database_url(env)
            with patch.dict(
                os.environ,
                {
                    "DB_HOST": "localhost",
                    "DB_USER": "test",
                    "DB_PASSWORD": "not-a-real-password",
                    "DB_PORT": "bad",
                },
            ):
                with self.assertRaisesRegex(ValueError, "DB_PORT"):
                    database_url(env)

    def test_isolate_test_and_development_database_names(self):
        for name in ("monow", "monow_ai", "postgres", "monow_ai_testx", None):
            with self.subTest(name=name), self.assertRaises(ValueError):
                validate_database_name(name, testing=True)
        for name in ("monow", "monow_ai_test", "postgres"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                validate_database_name(name)
        validate_database_name("monow_ai")
        validate_database_name("monow_ai_test", testing=True)
        validate_database_name("monow_ai_test_local", testing=True)

    def test_reject_wrong_database_before_starting_transaction(self):
        engine = Mock()
        engine.dialect.name = "postgresql"
        engine.url.database = "monow"

        with self.assertRaises(ValueError):
            import_glossary(engine, self.records)
        engine.begin.assert_not_called()

        with self.assertRaises(ValueError):
            create_database_engine("sqlite://")

    def test_fail_cli_for_invalid_database_configuration(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            env = Path(directory) / ".env"
            env.write_text("DB_NAME=monow_ai\n")
            stderr = io.StringIO()

            with redirect_stderr(stderr):
                self.assertEqual(main(["--env-file", str(env)]), 1)
            self.assertIn("DB 설정이 비어", stderr.getvalue())


class IdImportTests(unittest.TestCase):
    def test_keep_seven_json_fields_and_reject_database_id(self):
        from scripts.import_glossary import FIELDS

        records = load_glossary()
        self.assertEqual(len(records), 789)
        self.assertEqual(len(FIELDS), 7)
        self.assertNotIn("id", FIELDS)
        self.assertEqual(set(records[0]), FIELDS)
        with self.assertRaises(ValueError):
            validate_records([{**records[0], "id": 1}], expected_count=1)

    def test_exclude_database_id_from_json_comparison(self):
        record = load_glossary()[0]
        connection = Mock()
        connection.execute.return_value.mappings.return_value = [record]
        self.assertEqual(verify_records(connection, [record]), 1)
        statement = connection.execute.call_args.args[0]
        self.assertNotIn("id", statement.selected_columns.keys())
        self.assertEqual(len(statement.selected_columns), 7)

    def test_preserve_id_in_term_conflict_upsert(self):
        from sqlalchemy.dialects import postgresql
        from unittest.mock import MagicMock

        # Given
        engine = MagicMock()
        engine.dialect.name = "postgresql"
        engine.url.database = "monow_ai"
        connection = engine.begin.return_value.__enter__.return_value
        connection.scalar.side_effect = ["monow_ai", 789]
        result = connection.execute.return_value.scalars.return_value
        result.all.return_value = []
        records = load_glossary()
        before = deepcopy(records)

        # When
        with (
            patch.object(GlossaryTerm.__table__, "create"),
            patch("scripts.import_glossary.verify_records", return_value=789),
        ):
            report = import_glossary(engine, records)

        # Then
        self.assertEqual(report["total_count"], 789)
        self.assertEqual(records, before)
        for call in connection.execute.call_args_list:
            statement = call.args[0]
            sql = str(statement.compile(dialect=postgresql.dialect()))
            self.assertIn("ON CONFLICT (term) DO UPDATE SET", sql)
            update = sql.split("DO UPDATE SET", 1)[1]
            self.assertNotIn("id =", update)
            self.assertNotIn("id", statement.compile().params)


class PostgreSQLImportTests(unittest.TestCase):
    """TEST_DATABASE_URL 미지정 시 건너뛴다. 개발용 .env로 대체 접속하지 않는다."""

    @classmethod
    def setUpClass(cls):
        url = os.environ.get("TEST_DATABASE_URL")
        if not url:
            raise unittest.SkipTest("별도 TEST_DATABASE_URL 미지정: PostgreSQL 통합 테스트 미실행")
        cls.base_engine = create_database_engine(url, testing=True)
        cls.addClassCleanup(cls.base_engine.dispose)
        with cls.base_engine.connect() as connection:
            actual = connection.scalar(select(func.current_database()))
            validate_database_name(actual, testing=True)
            if actual != cls.base_engine.url.database:
                raise ValueError("테스트 DB 이름 불일치")
        cls.records = load_glossary()
        cls.source_bytes = DEFAULT_JSON.read_bytes()

    def setUp(self):
        # 테스트마다 고유 스키마만 생성/삭제하여 같은 테스트 DB 내의 다른 데이터도 보호한다.
        self.schema = "glossary_test_" + uuid4().hex
        self.engine = self.base_engine.execution_options(
            schema_translate_map={"public": self.schema}
        )

        with self.base_engine.begin() as connection:
            connection.execute(CreateSchema(self.schema))
        self.addCleanup(self.cleanup_schema)

    def cleanup_schema(self):
        with self.base_engine.begin() as connection:
            connection.execute(DropSchema(self.schema, cascade=True))
        self.assertEqual(DEFAULT_JSON.read_bytes(), self.source_bytes)

    def stored(self):
        with self.engine.connect() as connection:
            return {
                row["term"]: {key: value for key, value in row.items() if key != "id"}
                for row in connection.execute(select(GlossaryTerm.__table__)).mappings()
            }

    def test_import_and_verify_all_789_records(self):
        result = import_glossary(self.engine, self.records, testing=True)
        self.assertEqual(
            result,
            {"source_count": 789, "written_count": 789, "verified_count": 789, "total_count": 789},
        )
        self.assertEqual(self.stored(), {r["term"]: r for r in self.records})

    def test_skip_writes_for_identical_reimports(self):
        import_glossary(self.engine, self.records, testing=True)
        result = import_glossary(self.engine, self.records, testing=True)
        self.assertEqual(result["written_count"], 0)
        self.assertEqual(result["total_count"], 789)
        self.assertEqual(self.stored(), {r["term"]: r for r in self.records})

    def test_update_six_fields_for_changed_term(self):
        # Given
        import_glossary(self.engine, self.records, testing=True)
        changed = deepcopy(self.records)
        changed[0].update(
            definition="갱신 테스트: π_t^* × ∑x ≒ a÷b",
            formulas=[{"expression": "i^* ≠ i × 2"}],
            related_terms=["새 연관어"],
            source_pdf_page=38,
            source_book_page=None,
            review_status="needs_review",
        )

        # When
        result = import_glossary(self.engine, changed, testing=True)

        # Then
        self.assertEqual(result["written_count"], 1)
        self.assertEqual(result["total_count"], 789)
        self.assertEqual(self.stored(), {r["term"]: r for r in changed})

    def test_preserve_existing_terms_outside_import(self):
        extra = dict(self.records[0], term="격리된 테스트용 기존 용어")
        import_glossary(self.engine, [extra], expected_count=1, testing=True)
        result = import_glossary(self.engine, self.records, testing=True)
        self.assertEqual(result["verified_count"], 789)
        self.assertEqual(result["total_count"], 790)
        self.assertEqual(self.stored()[extra["term"]], extra)

    def test_rollback_all_batches_after_late_database_failure(self):
        # Given
        seed = dict(self.records[0], definition="실패 후에도 보존될 기존 설명")
        import_glossary(self.engine, [seed], expected_count=1, testing=True)
        self.assertEqual(self.records[-1]["term"], "V-KOSPI")
        qualified = self.base_engine.dialect.identifier_preparer.quote(self.schema)

        with self.engine.begin() as connection:
            connection.execute(
                text(
                    f"ALTER TABLE {qualified}.glossary_terms ADD CONSTRAINT "
                    "test_late_failure CHECK (term <> 'V-KOSPI')"
                )
            )
        # 마지막 배치에서 실제 CHECK 위반. 앞선 배치의 삽입과 기존 행 갱신도 복구되어야 한다.

        # When
        with self.assertRaises(IntegrityError):
            import_glossary(self.engine, self.records, testing=True)

        # Then
        self.assertEqual(self.stored(), {seed["term"]: seed})

    def test_rollback_rows_and_table_after_verification_mismatch(self):
        # Given
        original_verify = verify_records

        def mismatch(connection, records):
            altered = deepcopy(records)
            altered[0]["definition"] += "mismatch"
            return original_verify(connection, altered)

        # When
        with patch("scripts.import_glossary.verify_records", side_effect=mismatch):
            with self.assertRaisesRegex(ValueError, "일치하지"):
                import_glossary(self.engine, self.records, testing=True)
        with self.base_engine.connect() as connection:
            # Then
            self.assertFalse(inspect(connection).has_table("glossary_terms", schema=self.schema))

    def test_preserve_ids_and_explanations_during_reimport(self):
        from db.repositories.glossary_explanations import GlossaryExplanationRepository
        from llm.glossary_generator import definition_hash
        from scripts.generate_glossary_explanations import generate_for_term, prepare_table

        # Given
        import_glossary(self.engine, self.records, testing=True)
        prepare_table(self.engine, testing=True)

        def generate(term, definition):
            return {
                "explanation": "기존에 저장된 설명입니다.",
                "model": "mock-model",
                "prompt_version": "simple-v3-60",
                "definition_hash": definition_hash(definition),
            }

        generate_for_term(self.engine, "고용률", generate, testing=True)
        with self.engine.connect() as connection:
            ids = dict(
                connection.execute(select(GlossaryTerm.term, GlossaryTerm.id)).tuples().all()
            )
            saved = GlossaryExplanationRepository(connection).find("고용률")
        changed = deepcopy(self.records)
        changed[0]["definition"] += " 격리된 테스트 갱신."

        # When
        import_glossary(self.engine, changed, testing=True)
        import_glossary(self.engine, changed, testing=True)

        with self.engine.connect() as connection:
            # Then
            self.assertEqual(
                dict(connection.execute(select(GlossaryTerm.term, GlossaryTerm.id)).tuples().all()),
                ids,
            )
            self.assertEqual(GlossaryExplanationRepository(connection).find("고용률"), saved)
            self.assertEqual(saved["term_id"], ids["고용률"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
