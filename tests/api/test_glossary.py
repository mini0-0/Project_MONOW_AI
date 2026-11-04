from contextlib import ExitStack
from copy import deepcopy
import hashlib
import unittest
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from app.api.dependencies import get_engine, get_glossary_repository
from app.main import app
from app.services.glossary import SummaryUnavailableError, first_sentence
from db.models import GlossaryTerm
from db.repositories.glossary import GlossaryRepository
from tests.db.test_glossary_repository import isolated_glossary_database
from scripts.generate_glossary_explanations import (
    generate_for_term,
    prepare_table,
)

PATH = "/api/v1/ai/glossary/simple"
RECORD = {
    "term": "고용률",
    "definition": "고용률은 취업자가 차지하는 비율이다. 다음 설명이다.",
    "formulas": [],
    "related_terms": [],
    "review_status": "verified",
    "source_pdf_page": 38,
    "source_book_page": 20,
}


class SimpleGlossaryTests(unittest.TestCase):
    def test_given_spaced_thirty_chars_when_read_then_no_raw_length_limit(
        self,
    ):
        value = "가， " * 29 + "가."
        self.repository.find_simple_explanation.return_value["explanation"] = (
            value
        )
        response = self.client.get(PATH, params={"term": "고용률"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["summary"], value)

    def test_given_old_long_explanation_when_read_then_no_overwrite(self):
        stored = self.repository.find_simple_explanation.return_value
        stored["explanation"] = "가" * 31 + "."
        before = deepcopy(stored)
        with patch(
            "llm.client.GeminiClient.from_env", side_effect=AssertionError
        ):
            response = self.client.get(PATH, params={"term": "고용률"})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(stored, before)

    def setUp(self):
        get_engine.cache_clear()
        self.addCleanup(get_engine.cache_clear)

        self.repository = Mock(spec=GlossaryRepository)
        self.repository.find_by_term.return_value = deepcopy(RECORD)
        self.repository.find_simple_explanation.return_value = {
            "explanation": "15세 이상 인구에서 취업자가 차지하는 비율입니다.",
            "definition_hash": hashlib.sha256(
                RECORD["definition"].encode()
            ).hexdigest(),
        }
        app.dependency_overrides[get_glossary_repository] = (
            lambda: self.repository
        )
        self.addCleanup(app.dependency_overrides.clear)

        stack = ExitStack()
        self.addCleanup(stack.close)
        self.client = stack.enter_context(TestClient(app))

    def test_given_saved_simple_when_read_then_summary_is_returned(self):
        response = self.client.get(PATH, params={"term": " 고용률 "})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {
                "term": "고용률",
                "summary": "15세 이상 인구에서 취업자가 차지하는 비율입니다.",
                "review_status": "verified",
                "source_pdf_page": 38,
                "source_book_page": 20,
            },
        )
        self.repository.find_by_term.assert_called_once_with("고용률")
        self.assertEqual(self.repository.find_by_term.return_value, RECORD)

    def test_given_missing_term_when_read_then_404_is_returned(self):
        self.repository.find_by_term.return_value = None

        response = self.client.get(PATH, params={"term": "없는 용어"})

        self.assertEqual(response.status_code, 404)
        self.assertEqual(
            response.json()["detail"], "등록된 금융 용어를 찾을 수 없습니다"
        )

    def test_given_no_saved_simple_when_read_then_llm_is_not_called(self):
        self.repository.find_simple_explanation.return_value = None
        with patch(
            "llm.client.GeminiClient.from_env",
            side_effect=AssertionError("조회 중 생성 금지"),
        ):
            response = self.client.get(PATH, params={"term": "고용률"})
        self.assertEqual(response.status_code, 404)
        self.assertEqual(
            response.json()["detail"], "저장된 SIMPLE 설명이 없습니다"
        )

    def test_given_saved_simple_when_read_then_llm_is_not_called(self):
        with patch(
            "llm.client.GeminiClient.from_env",
            side_effect=AssertionError("조회 중 생성 금지"),
        ):
            response = self.client.get(PATH, params={"term": "고용률"})
        self.assertEqual(response.status_code, 200)
        self.repository.find_simple_explanation.assert_called_once_with(
            "고용률"
        )

    def test_given_invalid_term_when_requested_then_query_is_skipped(self):
        self.assertEqual(self.client.get(PATH).status_code, 422)

        for term in ("", " ", "a" * 201):
            with self.subTest(term=term):
                response = self.client.get(PATH, params={"term": term})
                self.assertEqual(response.status_code, 422)

        self.repository.find_by_term.assert_not_called()

    def test_given_review_status_when_read_then_metadata_is_preserved(self):
        self.repository.find_by_term.return_value.update(
            review_status="needs_review", source_book_page=None
        )

        response = self.client.get(PATH, params={"term": "고용률"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["review_status"], "needs_review")
        self.assertIsNone(response.json()["source_book_page"])

    def test_given_changed_source_when_read_then_409_is_returned(self):
        self.repository.find_by_term.return_value["definition"] = " "

        response = self.client.get(PATH, params={"term": "고용률"})

        self.assertEqual(response.status_code, 409)

    def test_given_db_failure_when_requested_then_secrets_are_hidden(self):
        app.dependency_overrides.clear()

        with patch(
            "app.api.dependencies.create_database_engine",
            side_effect=ValueError("password=secret"),
        ):
            response = self.client.get(PATH, params={"term": "고용률"})
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("secret", response.text)

        engine = Mock()
        engine.connect.side_effect = SQLAlchemyError("password=secret")
        with patch("app.api.dependencies.get_engine", return_value=engine):
            response = self.client.get(PATH, params={"term": "고용률"})
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("secret", response.text)

    def test_given_no_db_when_health_checked_then_legacy_app_works(self):
        from main import app as legacy_app

        self.assertIs(app, legacy_app)
        with patch(
            "app.api.dependencies.create_database_engine",
            side_effect=AssertionError("DB 접속 금지"),
        ):
            self.assertEqual(
                self.client.get("/health").json(), {"status": "ok"}
            )


class FirstSentenceTests(unittest.TestCase):
    def test_given_abbreviations_when_split_then_decimals_are_preserved(self):
        text = "금리(Interest, e.g. 0.5)는 비율이다. 다음 문장이다."
        self.assertEqual(
            first_sentence(text), "금리(Interest, e.g. 0.5)는 비율이다."
        )

    def test_given_newlines_when_split_then_text_is_not_truncated(self):
        self.assertEqual(
            first_sentence("첫\n설명이다.\n다음 설명이다."), "첫 설명이다."
        )
        long_sentence = "설명" * 150 + "이다."
        self.assertEqual(
            first_sentence(long_sentence + " 다음 문장이다."), long_sentence
        )
        self.assertEqual(
            first_sentence("종결 부호 없는 설명"), "종결 부호 없는 설명"
        )

    def test_given_empty_definition_when_split_then_error_is_raised(self):
        with self.assertRaises(SummaryUnavailableError):
            first_sentence(" \n ")


class SimpleGlossaryPostgreSQLTests(unittest.TestCase):
    def test_given_postgres_records_when_read_then_source_is_unchanged(self):
        with isolated_glossary_database() as (engine, records):
            prepare_table(engine, testing=True)

            def generate(term, definition):
                return {
                    "explanation": "초보자를 위한 저장된 설명입니다.",
                    "model": "mock-model",
                    "prompt_version": "test-v1",
                    "definition_hash": hashlib.sha256(
                        definition.encode()
                    ).hexdigest(),
                }

            for term in ("고용률", "주가수익비율(PER)"):
                generate_for_term(engine, term, generate, testing=True)

            def repository():
                with engine.connect() as connection:
                    yield GlossaryRepository(connection)

            app.dependency_overrides[get_glossary_repository] = repository

            try:
                with TestClient(app) as client:
                    for title in ("고용률", "주가수익비율(PER)"):
                        response = client.get(PATH, params={"term": title})
                        expected = next(
                            r for r in records if r["term"] == title
                        )

                        self.assertEqual(response.status_code, 200)
                        self.assertEqual(response.json()["term"], title)
                        self.assertEqual(
                            response.json()["summary"],
                            "초보자를 위한 저장된 설명입니다.",
                        )
                        self.assertEqual(
                            response.json()["review_status"],
                            expected["review_status"],
                        )
                    self.assertEqual(
                        client.get(
                            PATH, params={"term": "없는 용어"}
                        ).status_code,
                        404,
                    )
                with engine.connect() as connection:
                    actual = [
                        dict(r)
                        for r in connection.execute(
                            select(GlossaryTerm.__table__)
                        ).mappings()
                    ]
                self.assertEqual(
                    {r["term"]: r for r in actual},
                    {r["term"]: r for r in records},
                )
            finally:
                app.dependency_overrides.clear()
