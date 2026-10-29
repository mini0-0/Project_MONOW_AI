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
from scripts.import_glossary import (DEFAULT_JSON, import_glossary, load_glossary,
                                    main, validate_records, verify_records)


class ImportValidationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.records = load_glossary()

    def test_original_789_records_and_nested_expressions_not_modified(self):
        original = deepcopy(self.records)
        validate_records(self.records)
        self.assertEqual(len(self.records), 789)
        self.assertEqual(sum(len(r["formulas"]) for r in self.records), 59)
        self.assertEqual(self.records, original)

    def test_count_and_duplicate_terms_rejected(self):
        with self.assertRaisesRegex(ValueError, "789"):
            validate_records(self.records[:-1])
        records = [self.records[0], self.records[0]]
        with self.assertRaisesRegex(ValueError, "중복 term"):
            validate_records(records, expected_count=2)

    def test_invalid_fields_values_and_nested_data_rejected(self):
        for field, value in (("definition", " "), ("term", "a\x00b"),
                             ("formulas", [{"expression": "x", "type": "formula"}]),
                             ("formulas", [{"expression": ""}]), ("formulas", None),
                             ("related_terms", [1]), ("source_pdf_page", True),
                             ("source_pdf_page", 0), ("source_pdf_page", 2 ** 31),
                             ("source_book_page", -1), ("review_status", "unknown")):
            with self.subTest(field=field, value=value):
                record = dict(self.records[0], **{field: value})
                with self.assertRaises(ValueError):
                    validate_records([record], expected_count=1)
        for record in ({k: v for k, v in self.records[0].items() if k != "definition"},
                       dict(self.records[0], unexpected="x")):
            with self.assertRaises(ValueError):
                validate_records([record], expected_count=1)

    def test_unknown_book_page_kept_as_null(self):
        record = dict(self.records[0], source_book_page=None)
        validate_records([record], expected_count=1)
        self.assertIsNone(record["source_book_page"])

    def test_duplicate_json_keys_and_malformed_json_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "input.json"
            for content in ('[{"term":"a","term":"b"}]', '{bad json'):
                path.write_text(content)
                with self.assertRaises(ValueError):
                    load_glossary(path)

    def test_env_password_special_characters_and_environment_precedence(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            env = Path(directory) / ".env"
            env.write_text("DB_HOST=localhost\nDB_PORT=5432\nDB_NAME=monow_ai\n"
                           "DB_USER=test_user\nDB_PASSWORD='p@ss:/#word'\n")
            url = database_url(env)
            self.assertEqual(url.password, "p@ss:/#word")
            self.assertNotIn(url.password, str(url))
            self.assertNotIn("DB_PASSWORD", os.environ)
            with patch.dict(os.environ, {"DB_HOST": "override"}):
                self.assertEqual(database_url(env).host, "override")

    def test_missing_configuration_and_invalid_port_rejected(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            env = Path(directory) / ".env"
            env.write_text("DB_NAME=monow_ai\n")
            with self.assertRaisesRegex(ValueError, "DB_USER"):
                database_url(env)
            with patch.dict(os.environ, {"DB_HOST": "localhost", "DB_USER": "test",
                                         "DB_PASSWORD": "not-a-real-password", "DB_PORT": "bad"}):
                with self.assertRaisesRegex(ValueError, "DB_PORT"):
                    database_url(env)

    def test_development_and_other_db_names_rejected_in_tests(self):
        for name in ("monow", "monow_ai", "postgres", "monow_ai_testx", None):
            with self.subTest(name=name), self.assertRaises(ValueError):
                validate_database_name(name, testing=True)
        for name in ("monow", "monow_ai_test", "postgres"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                validate_database_name(name)
        validate_database_name("monow_ai")
        validate_database_name("monow_ai_test", testing=True)
        validate_database_name("monow_ai_test_local", testing=True)

    def test_wrong_database_rejected_before_transaction(self):
        engine = Mock()
        engine.dialect.name = "postgresql"
        engine.url.database = "monow"
        with self.assertRaises(ValueError):
            import_glossary(engine, self.records)
        engine.begin.assert_not_called()
        with self.assertRaises(ValueError):
            create_database_engine("sqlite://")

    def test_cli_failure_returns_nonzero_without_connecting(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            env = Path(directory) / ".env"
            env.write_text("DB_NAME=monow_ai\n")
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                self.assertEqual(main(["--env-file", str(env)]), 1)
            self.assertIn("DB 설정이 비어", stderr.getvalue())


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
        self.engine = self.base_engine.execution_options(schema_translate_map={"public": self.schema})
        with self.base_engine.begin() as connection:
            connection.execute(CreateSchema(self.schema))
        self.addCleanup(self.cleanup_schema)

    def cleanup_schema(self):
        with self.base_engine.begin() as connection:
            connection.execute(DropSchema(self.schema, cascade=True))
        self.assertEqual(DEFAULT_JSON.read_bytes(), self.source_bytes)

    def stored(self):
        with self.engine.connect() as connection:
            return {row["term"]: dict(row) for row in connection.execute(select(GlossaryTerm.__table__)).mappings()}

    def test_full_789_import_and_every_json_field_matches(self):
        result = import_glossary(self.engine, self.records, testing=True)
        self.assertEqual(result, {"source_count": 789, "written_count": 789,
                                  "verified_count": 789, "total_count": 789})
        self.assertEqual(self.stored(), {r["term"]: r for r in self.records})

    def test_repeat_import_has_no_duplicates_or_unnecessary_updates(self):
        import_glossary(self.engine, self.records, testing=True)
        result = import_glossary(self.engine, self.records, testing=True)
        self.assertEqual(result["written_count"], 0)
        self.assertEqual(result["total_count"], 789)
        self.assertEqual(self.stored(), {r["term"]: r for r in self.records})

    def test_upsert_updates_all_six_non_key_fields(self):
        import_glossary(self.engine, self.records, testing=True)
        changed = deepcopy(self.records)
        changed[0].update(definition="갱신 테스트: π_t^* × ∑x ≒ a÷b",
                          formulas=[{"expression": "i^* ≠ i × 2"}], related_terms=["새 연관어"],
                          source_pdf_page=38, source_book_page=None, review_status="needs_review")
        result = import_glossary(self.engine, changed, testing=True)
        self.assertEqual(result["written_count"], 1)
        self.assertEqual(result["total_count"], 789)
        self.assertEqual(self.stored(), {r["term"]: r for r in changed})

    def test_existing_terms_not_in_source_are_not_deleted(self):
        extra = dict(self.records[0], term="격리된 테스트용 기존 용어")
        import_glossary(self.engine, [extra], expected_count=1, testing=True)
        result = import_glossary(self.engine, self.records, testing=True)
        self.assertEqual(result["verified_count"], 789)
        self.assertEqual(result["total_count"], 790)
        self.assertEqual(self.stored()[extra["term"]], extra)

    def test_late_database_failure_rolls_back_prior_inserts_and_updates(self):
        seed = dict(self.records[0], definition="실패 후에도 보존될 기존 설명")
        import_glossary(self.engine, [seed], expected_count=1, testing=True)
        self.assertEqual(self.records[-1]["term"], "V-KOSPI")
        qualified = self.base_engine.dialect.identifier_preparer.quote(self.schema)
        with self.engine.begin() as connection:
            connection.execute(text(f"ALTER TABLE {qualified}.glossary_terms ADD CONSTRAINT "
                                    "test_late_failure CHECK (term <> 'V-KOSPI')"))
        # 마지막 배치에서 실제 CHECK 위반. 앞선 배치의 삽입과 기존 행 갱신도 복구되어야 한다.
        with self.assertRaises(IntegrityError):
            import_glossary(self.engine, self.records, testing=True)
        self.assertEqual(self.stored(), {seed["term"]: seed})

    def test_comparison_mismatch_rolls_back_table_and_all_rows(self):
        original_verify = verify_records

        def mismatch(connection, records):
            altered = deepcopy(records)
            altered[0]["definition"] += "mismatch"
            return original_verify(connection, altered)

        with patch("scripts.import_glossary.verify_records", side_effect=mismatch):
            with self.assertRaisesRegex(ValueError, "일치하지"):
                import_glossary(self.engine, self.records, testing=True)
        with self.base_engine.connect() as connection:
            self.assertFalse(inspect(connection).has_table("glossary_terms", schema=self.schema))


if __name__ == "__main__":
    unittest.main(verbosity=2)
