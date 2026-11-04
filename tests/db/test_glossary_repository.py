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

    def test_given_exact_term_when_queried_then_source_is_unchanged(self):
        with self.engine.connect() as connection:
            repository = GlossaryRepository(connection)
            for title in ("고용률", "주가수익비율(PER)", "로렌츠곡선"):
                expected = next(row for row in self.records if row["term"] == title)
                self.assertEqual(repository.find_by_term(title), expected)
            self.assertEqual(repository.find_by_term("없는 용어"), None)
        with self.engine.connect() as connection:
            actual = [dict(row) for row in connection.execute(select(GlossaryTerm.__table__)).mappings()]
        self.assertEqual({r["term"]: r for r in actual}, {r["term"]: r for r in self.records})

    def test_given_sql_in_term_when_queried_then_values_are_bound(self):
        with self.engine.connect() as connection:
            repository = GlossaryRepository(connection)
            self.assertIsNone(repository.find_by_term("' OR 1=1 --"))
            self.assertEqual(connection.scalar(select(func.count()).select_from(GlossaryTerm)), 789)
