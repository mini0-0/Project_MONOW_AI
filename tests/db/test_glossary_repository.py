from contextlib import ExitStack, contextmanager
import os
import unittest
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.schema import CreateSchema, DropSchema

from db.database import create_database_engine, validate_database_name
from db.models import GlossaryTerm
from db.repositories.glossary import GlossaryRepository
from scripts.import_glossary import import_glossary, load_glossary


@contextmanager
def isolated_glossary_database():
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        raise unittest.SkipTest("TEST_DATABASE_URL 미지정: 별도 PostgreSQL 테스트 미실행")
    engine = create_database_engine(url, testing=True)
    schema = "glossary_test_" + uuid4().hex
    created = False

    try:
        with engine.begin() as connection:
            actual = connection.scalar(select(func.current_database()))
            validate_database_name(actual, testing=True)
            if actual != engine.url.database:
                raise ValueError("테스트 DB 이름 불일치")
            connection.execute(CreateSchema(schema))
            created = True
        isolated = engine.execution_options(schema_translate_map={"public": schema})
        records = load_glossary()
        import_glossary(isolated, records, testing=True)
        yield isolated, records

    finally:
        if created:
            with engine.begin() as connection:
                connection.execute(DropSchema(schema, cascade=True))
        engine.dispose()


class GlossaryRepositoryTests(unittest.TestCase):
    def setUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        self.engine, self.records = stack.enter_context(isolated_glossary_database())

    def test_find_exact_term_without_changing_source(self):
        with self.engine.connect() as connection:
            repository = GlossaryRepository(connection)

            for title in ("고용률", "주가수익비율(PER)", "로렌츠곡선"):
                expected = next(row for row in self.records if row["term"] == title)
                stored = repository.find_by_term(title)
                self.assertIsInstance(stored["id"], int)
                self.assertEqual(
                    {key: value for key, value in stored.items() if key != "id"}, expected
                )
            self.assertEqual(repository.find_by_term("없는 용어"), None)

        with self.engine.connect() as connection:
            actual = [
                dict(row) for row in connection.execute(select(GlossaryTerm.__table__)).mappings()
            ]
        self.assertEqual(
            {r["term"]: {k: v for k, v in r.items() if k != "id"} for r in actual},
            {r["term"]: r for r in self.records},
        )

    def test_bind_sql_like_term_as_query_parameter(self):
        with self.engine.connect() as connection:
            repository = GlossaryRepository(connection)
            self.assertIsNone(repository.find_by_term("' OR 1=1 --"))
            self.assertEqual(connection.scalar(select(func.count()).select_from(GlossaryTerm)), 789)


class IdSchemaMappingTests(unittest.TestCase):
    def test_map_bigint_primary_keys_and_fk_unique_constraints(self):
        from sqlalchemy import BigInteger, UniqueConstraint
        from db.models import GlossaryExplanation

        source = GlossaryTerm.__table__
        saved = GlossaryExplanation.__table__

        for table in (source, saved):
            self.assertEqual(list(table.primary_key.columns.keys()), ["id"])
            self.assertIsInstance(table.c.id.type, BigInteger)
            self.assertTrue(table.c.id.autoincrement)
        self.assertTrue(source.c.term.unique)
        self.assertNotIn("term", saved.c)
        self.assertIsInstance(saved.c.term_id.type, BigInteger)
        self.assertFalse(saved.c.term_id.nullable)
        self.assertEqual(
            {fk.target_fullname for fk in saved.c.term_id.foreign_keys},
            {"public.glossary_terms.id"},
        )
        unique_sets = {
            tuple(item.columns.keys())
            for item in saved.constraints
            if isinstance(item, UniqueConstraint)
        }
        self.assertIn(("term_id", "explanation_type"), unique_sets)

    def test_join_explanation_by_id_and_bind_term_parameter(self):
        from unittest.mock import Mock
        from sqlalchemy.dialects import postgresql
        from db.repositories.glossary_explanations import GlossaryExplanationRepository

        # Given
        connection = Mock()
        row = {"id": 2**40, "term_id": 2**41, "explanation_type": "SIMPLE"}
        result = connection.execute.return_value.mappings.return_value
        result.one_or_none.return_value = row
        repository = GlossaryExplanationRepository(connection)
        title = "' OR 1=1 --"
        self.assertEqual(repository.find(title), row)

        # When
        statement = connection.execute.call_args.args[0]
        sql = str(statement.compile(dialect=postgresql.dialect()))

        # Then
        self.assertIn("glossary_explanations.term_id = public.glossary_terms.id", sql)
        self.assertIn(title, statement.compile().params.values())
        self.assertNotIn(title, sql)
        self.assertIn("SIMPLE", statement.compile().params.values())

    def test_correlate_missing_explanations_by_fk_id(self):
        from unittest.mock import Mock
        from sqlalchemy.dialects import postgresql
        from db.repositories.glossary_explanations import GlossaryExplanationRepository

        connection = Mock()
        connection.scalars.return_value = ["국채"]
        self.assertEqual(GlossaryExplanationRepository(connection).missing_terms(), ["국채"])
        statement = connection.scalars.call_args.args[0]
        sql = str(statement.compile(dialect=postgresql.dialect()))
        self.assertIn("NOT (EXISTS", sql)
        self.assertIn("glossary_explanations.term_id = public.glossary_terms.id", sql)
        self.assertIn("SIMPLE", statement.compile().params.values())
        self.assertIn("needs_review", statement.compile().params.values())

    def test_ignore_duplicate_explanations_by_fk_and_type(self):
        from unittest.mock import Mock
        from sqlalchemy.dialects import postgresql
        from db.repositories.glossary_explanations import GlossaryExplanationRepository

        # Given
        connection = Mock()
        repository = GlossaryExplanationRepository(connection)
        record = {
            "term_id": 2**40,
            "explanation_type": "SIMPLE",
            "explanation": "원본에 근거한 설명입니다.",
            "model": "mock-model",
            "prompt_version": "simple-v3-60",
            "definition_hash": "a" * 64,
        }
        original = dict(record)
        result = connection.execute.return_value
        result.scalar_one_or_none.side_effect = [2**42, None]
        self.assertTrue(repository.save_if_absent(record))
        self.assertFalse(repository.save_if_absent(record))

        # When
        statement = connection.execute.call_args.args[0]
        sql = str(statement.compile(dialect=postgresql.dialect()))

        # Then
        self.assertIn("ON CONFLICT (term_id, explanation_type) DO NOTHING", sql)
        self.assertIn("RETURNING public.glossary_explanations.id", sql)
        self.assertNotIn("DO UPDATE", sql)
        self.assertEqual(statement.compile().params["term_id"], 2**40)
        self.assertEqual(record, original)
