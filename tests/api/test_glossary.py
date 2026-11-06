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
from app.schemas.glossary import SimpleGlossaryResponse
from app.services.glossary import simple_glossary
from db.models import GlossaryTerm
from db.repositories.glossary import GlossaryRepository
from tests.db.test_glossary_repository import isolated_glossary_database
from scripts.generate_glossary_explanations import generate_for_term, prepare_table

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
    def test_accept_sixty_characters_excluding_spaces_and_punctuation(self):
        value = "가， " * 59 + "가."
        self.repository.find_simple_explanation.return_value["explanation"] = value
        response = self.client.get(PATH, params={"term": "고용률"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["summary"], value)

    def test_reject_oversized_stored_explanation_without_overwriting(self):
        stored = self.repository.find_simple_explanation.return_value
        stored["explanation"] = "가" * 61 + "."
        before = deepcopy(stored)
        with patch("llm.client.GeminiClient.from_env", side_effect=AssertionError):
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
            "definition_hash": hashlib.sha256(RECORD["definition"].encode()).hexdigest(),
        }
        app.dependency_overrides[get_glossary_repository] = lambda: self.repository
        self.addCleanup(app.dependency_overrides.clear)

        stack = ExitStack()
        self.addCleanup(stack.close)
        self.client = stack.enter_context(TestClient(app))

    def test_return_stored_simple_explanation(self):
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

    def test_return_404_for_unknown_term(self):
        self.repository.find_by_term.return_value = None

        response = self.client.get(PATH, params={"term": "없는 용어"})

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["detail"], "등록된 금융 용어를 찾을 수 없습니다")

    def test_return_404_without_calling_llm_for_missing_explanation(self):
        self.repository.find_simple_explanation.return_value = None
        with patch(
            "llm.client.GeminiClient.from_env", side_effect=AssertionError("조회 중 생성 금지")
        ):
            response = self.client.get(PATH, params={"term": "고용률"})
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["detail"], "저장된 SIMPLE 설명이 없습니다")

    def test_return_saved_explanation_without_calling_llm(self):
        with patch(
            "llm.client.GeminiClient.from_env", side_effect=AssertionError("조회 중 생성 금지")
        ):
            response = self.client.get(PATH, params={"term": "고용률"})
        self.assertEqual(response.status_code, 200)
        self.repository.find_simple_explanation.assert_called_once_with("고용률")

    def test_reject_invalid_term_before_database_lookup(self):
        self.assertEqual(self.client.get(PATH).status_code, 422)

        for term in ("", " ", "a" * 201):
            with self.subTest(term=term):
                response = self.client.get(PATH, params={"term": term})
                self.assertEqual(response.status_code, 422)

        self.repository.find_by_term.assert_not_called()

    def test_preserve_review_status_in_response(self):
        self.repository.find_by_term.return_value.update(
            review_status="needs_review", source_book_page=None
        )

        response = self.client.get(PATH, params={"term": "고용률"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["review_status"], "needs_review")
        self.assertIsNone(response.json()["source_book_page"])

    def test_return_409_for_changed_source_definition(self):
        self.repository.find_by_term.return_value["definition"] = " "

        response = self.client.get(PATH, params={"term": "고용률"})

        self.assertEqual(response.status_code, 409)

    def test_hide_secrets_in_database_failure_responses(self):
        # Given
        app.dependency_overrides.clear()

        # When
        with patch(
            "app.api.dependencies.create_database_engine", side_effect=ValueError("password=secret")
        ):
            response = self.client.get(PATH, params={"term": "고용률"})
        # Then
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("secret", response.text)

        # Given
        engine = Mock()
        engine.connect.side_effect = SQLAlchemyError("password=secret")

        # When
        with patch("app.api.dependencies.get_engine", return_value=engine):
            response = self.client.get(PATH, params={"term": "고용률"})

        # Then
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("secret", response.text)

    def test_serve_health_without_database_and_keep_legacy_app(self):
        from main import app as legacy_app

        self.assertIs(app, legacy_app)
        with patch(
            "app.api.dependencies.create_database_engine",
            side_effect=AssertionError("DB 접속 금지"),
        ):
            self.assertEqual(self.client.get("/health").json(), {"status": "ok"})


class StoredExplanationCompatibilityTests(unittest.TestCase):
    def response(self, text):
        return SimpleGlossaryResponse(
            term="고용률",
            summary=text,
            review_status="verified",
            source_pdf_page=38,
            source_book_page=20,
        )

    def test_accept_existing_thirty_character_explanation(self):
        text = "가" * 30 + "."
        self.assertEqual(self.response(text).summary, text)

    def test_keep_response_schema_and_source_without_calling_llm(self):
        # Given
        repository = Mock(spec=GlossaryRepository)
        source = {**deepcopy(RECORD), "id": 2**40}
        repository.find_by_term.return_value = source
        record = {
            "id": 2**41,
            "term_id": 2**40,
            "explanation": "가， " * 59 + "가.",
            "definition_hash": hashlib.sha256(RECORD["definition"].encode()).hexdigest(),
            "prompt_version": "simple-v2-30",
        }
        repository.find_simple_explanation.return_value = record
        before = deepcopy(record)

        # When
        with patch("llm.client.GeminiClient.from_env", side_effect=AssertionError):
            response = simple_glossary(repository, "고용률")
        # Then
        self.assertEqual(response.summary, record["explanation"])
        self.assertEqual(record, before)
        self.assertEqual(repository.find_by_term.return_value, source)
        self.assertEqual(
            set(response.model_dump()),
            {"term", "summary", "review_status", "source_pdf_page", "source_book_page"},
        )

    def test_reject_sixty_one_character_response(self):
        from pydantic import ValidationError

        with self.assertRaises(ValidationError):
            self.response("가" * 61 + ".")


class SimpleGlossaryPostgreSQLTests(unittest.TestCase):
    def test_serve_postgresql_explanations_without_changing_source(self):
        # Given
        with isolated_glossary_database() as (engine, records):
            prepare_table(engine, testing=True)

            def generate(term, definition):
                return {
                    "explanation": "초보자를 위한 저장된 설명입니다.",
                    "model": "mock-model",
                    "prompt_version": "test-v1",
                    "definition_hash": hashlib.sha256(definition.encode()).hexdigest(),
                }

            for term in ("고용률", "주가수익비율(PER)"):
                generate_for_term(engine, term, generate, testing=True)

            def repository():
                with engine.connect() as connection:
                    yield GlossaryRepository(connection)

            app.dependency_overrides[get_glossary_repository] = repository

            try:
                # When
                with TestClient(app) as client:
                    for title in ("고용률", "주가수익비율(PER)"):
                        response = client.get(PATH, params={"term": title})
                        expected = next(r for r in records if r["term"] == title)

                        # Then
                        self.assertEqual(response.status_code, 200)
                        self.assertEqual(response.json()["term"], title)
                        self.assertEqual(
                            response.json()["summary"], "초보자를 위한 저장된 설명입니다."
                        )
                        self.assertEqual(
                            response.json()["review_status"], expected["review_status"]
                        )
                    self.assertEqual(
                        client.get(PATH, params={"term": "없는 용어"}).status_code, 404
                    )
                with engine.connect() as connection:
                    actual = [
                        dict(r)
                        for r in connection.execute(select(GlossaryTerm.__table__)).mappings()
                    ]
                self.assertEqual(
                    {r["term"]: {k: v for k, v in r.items() if k != "id"} for r in actual},
                    {r["term"]: r for r in records},
                )
            finally:
                app.dependency_overrides.clear()
