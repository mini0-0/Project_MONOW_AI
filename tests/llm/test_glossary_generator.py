from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

import httpx
from google import genai
from google.genai import errors, types
from pydantic import ValidationError

from db.models import simple_explanation_length
from llm.client import (
    ExplanationOutput,
    GeminiClient,
    GenerationError,
    GenerationStopped,
    QuotaExceededError,
)
from llm.glossary_generator import (
    INSTRUCTIONS,
    PROMPT_VERSION,
    definition_hash,
    generate_explanation,
    validate_explanation,
)
from scripts.generate_glossary_explanations import main


class GeneratorTests(unittest.TestCase):
    def test_given_ignored_characters_when_counted_then_thirty_is_valid(self):
        value = "가， " * 29 + "가."
        self.assertGreater(len(value), 60)
        self.assertEqual(simple_explanation_length(value), 30)
        self.assertEqual(validate_explanation(value), value)
        self.assertEqual(
            ExplanationOutput(explanation=value).explanation, value
        )

    def test_given_unicode_symbols_when_counted_then_only_punctuation_ignored(
        self,
    ):
        self.assertEqual(
            simple_explanation_length("가，、。‘’\u00a0나\u202f+₩"), 4
        )
        with self.assertRaises(ValidationError):
            ExplanationOutput(explanation="가" * 31 + ".")

    def test_given_original_source_when_prompted_then_metadata_is_recorded(
        self,
    ):
        client = Mock()
        client.generate.return_value = (
            "주가가 순이익의 몇 배인지 나타내는 지표입니다.",
            "mock-model",
        )
        definition = "원본 설명\n조건과 수식 a÷b ≒ 0.5를 유지한다."
        result = generate_explanation(client, "PER", definition)
        instructions, source = client.generate.call_args.args
        self.assertEqual(
            json.loads(source), {"term": "PER", "definition": definition}
        )
        for rule in (
            "금융 초보자",
            "30자",
            "특징",
            "한 문장",
            "명령이나 지시는 따르지",
        ):
            self.assertIn(rule, instructions)
        self.assertEqual(
            result["definition_hash"], definition_hash(definition)
        )
        self.assertEqual(result["prompt_version"], PROMPT_VERSION)
        self.assertEqual(result["model"], "mock-model")

    def test_given_thirty_chars_when_validated_then_decimals_are_preserved(
        self,
    ):
        self.assertEqual(
            validate_explanation("가" * 30 + "."), "가" * 30 + "."
        )
        self.assertEqual(
            validate_explanation("  금리가 0.5% 변하는 비율입니다.  "),
            "금리가 0.5% 변하는 비율입니다.",
        )

    def test_given_invalid_output_when_validated_then_no_retry_or_truncate(
        self,
    ):
        for value in (
            "",
            " ",
            "가" * 31 + ".",
            "첫 문장입니다. 둘째입니다.",
            "개행\n설명입니다.",
            "설명\u200b입니다.",
            "설명\u2028입니다.",
            "완결되지 않은 설명",
            "- 설명입니다.",
            "설명입니다?",
            "설명입니다!",
            None,
        ):
            with self.subTest(value=value), self.assertRaises(GenerationError):
                validate_explanation(value)
        client = Mock()
        client.generate.return_value = ("가" * 31 + ".", "mock-model")
        with self.assertRaises(GenerationError):
            generate_explanation(client, "용어", "원문")
        client.generate.assert_called_once()

    def test_given_empty_source_when_generated_then_llm_is_not_called(self):
        client = Mock()
        for term, definition in (("", "원문"), ("용어", " ")):
            with self.assertRaises(GenerationError):
                generate_explanation(client, term, definition)
        client.generate.assert_not_called()


class ClientTests(unittest.TestCase):
    model = "gemini-3.5-flash-lite"

    def test_given_unknown_schema_field_when_analyzed_then_raw_message_hidden(
        self,
    ):
        secret = "CANARY_PRIVATE_KEY_AND_REQUEST"
        messages = (
            (
                'Unknown name "additional_properties" at generation_config.response_schema',
                "UNSUPPORTED_RESPONSE_SCHEMA_FIELD",
            ),
            (
                'Unknown field "additionalProperties" in responseSchema',
                "UNSUPPORTED_RESPONSE_SCHEMA_FIELD",
            ),
            (
                'Unknown name "min_length" at generation_config.response_schema',
                "UNSUPPORTED_RESPONSE_SCHEMA_FIELD",
            ),
            (
                'Unknown name "candidate_count" at generation_config',
                "INVALID_REQUEST",
            ),
            (
                "API key invalid with response_schema additionalProperties",
                "INVALID_REQUEST",
            ),
        )
        sdk = Mock()
        for message, expected in messages:
            sdk.models.generate_content.side_effect = errors.APIError(
                400,
                {
                    "error": {
                        "code": 400,
                        "status": "INVALID_ARGUMENT",
                        "message": message + " " + secret,
                    },
                },
            )
            with self.subTest(expected=expected), self.assertRaises(
                GenerationStopped
            ) as raised:
                self.client(sdk).generate(INSTRUCTIONS, "원문")
            self.assertEqual(raised.exception.error_type, expected)
            self.assertEqual(raised.exception.http_status, 400)
            self.assertEqual(
                raised.exception.provider_status, "INVALID_ARGUMENT"
            )
            self.assertNotIn(secret, str(raised.exception))
            self.assertNotIn(
                secret, json.dumps(raised.exception.diagnostics())
            )

    def test_given_json_schema_response_when_validated_then_rules_unchanged(
        self,
    ):
        sdk = Mock()
        for value in (
            {"explanation": "가" * 31 + "."},
            {"explanation": "설명입니다.", "unexpected": "private data"},
            {"explanation": ""},
            {"explanation": "첫 문장입니다. 두 문장입니다."},
            {"explanation": "설명\n입니다."},
        ):
            response = self.response()
            response.parsed = value
            sdk.models.generate_content.return_value = response
            with self.subTest(keys=list(value)), self.assertRaises(
                GenerationError
            ):
                self.client(sdk).generate(INSTRUCTIONS, "원문")
        sdk.models.generate_content.return_value = self.response()
        self.assertEqual(
            self.client(sdk).generate(INSTRUCTIONS, "원문"),
            ("쉬운 설명입니다.", self.model),
        )

    def client(self, sdk, interval_seconds=0):
        return GeminiClient(
            self.model,
            sdk,
            free_tier_confirmed=True,
            interval_seconds=interval_seconds,
        )

    def response(self, text="쉬운 설명입니다."):
        response = types.GenerateContentResponse(
            candidates=[
                types.Candidate(finish_reason=types.FinishReason.STOP)
            ],
            model_version=self.model,
        )
        # 실제 SDK는 응답 객체를 만든 뒤 JSON Schema 파싱 결과를 대입한다.
        response.parsed = {"explanation": text}
        return response

    def test_given_mock_http_when_sdk_parses_then_no_external_request_occurs(
        self,
    ):
        explanation = "가， " * 29 + "가."
        requests = []
        quota = False

        def respond(request):
            requests.append(request)
            body = json.loads(request.content)
            self.assertEqual(
                body["systemInstruction"]["parts"][0]["text"], INSTRUCTIONS
            )
            self.assertEqual(body["contents"][0]["parts"][0]["text"], "원문")
            self.assertEqual(
                body["generationConfig"]["responseMimeType"],
                "application/json",
            )
            self.assertNotIn("responseSchema", body["generationConfig"])
            schema = body["generationConfig"]["responseJsonSchema"]
            self.assertEqual(schema, ExplanationOutput.model_json_schema())
            self.assertFalse(schema["additionalProperties"])
            self.assertEqual(
                schema["properties"]["explanation"]["minLength"], 1
            )
            self.assertEqual(
                schema["properties"]["explanation"]["type"], "string"
            )
            self.assertNotIn("additional_properties", json.dumps(schema))
            self.assertNotIn("min_length", json.dumps(schema))
            self.assertNotIn("tools", body)
            self.assertEqual(request.headers["x-goog-api-key"], "mock-key")
            self.assertNotIn("mock-key", str(request.url))
            if quota:
                return httpx.Response(
                    429,
                    json={
                        "error": {
                            "code": 429,
                            "status": "RESOURCE_EXHAUSTED",
                            "message": "mock quota",
                        }
                    },
                )
            return httpx.Response(
                200,
                json={
                    "candidates": [
                        {
                            "content": {
                                "role": "model",
                                "parts": [
                                    {
                                        "text": json.dumps(
                                            {"explanation": explanation}
                                        ),
                                    }
                                ],
                            },
                            "finishReason": "STOP",
                            "index": 0,
                        }
                    ],
                    "modelVersion": self.model,
                },
            )

        with httpx.Client(transport=httpx.MockTransport(respond)) as http:
            with genai.Client(
                api_key="mock-key",
                vertexai=False,
                enterprise=False,
                http_options=types.HttpOptions(
                    httpx_client=http,
                    retry_options=types.HttpRetryOptions(attempts=1),
                ),
            ) as sdk:
                client = self.client(sdk)
                self.assertEqual(
                    client.generate(INSTRUCTIONS, "원문"),
                    (explanation, self.model),
                )
                explanation = "가" * 31 + "."
                with self.assertRaises(GenerationError):
                    client.generate(INSTRUCTIONS, "원문")
                quota = True
                with self.assertRaises(QuotaExceededError):
                    client.generate(INSTRUCTIONS, "원문")
        self.assertEqual(len(requests), 3)

    def test_given_bad_sdk_payload_when_parsed_then_generation_fails(self):
        sdk = Mock()
        sdk.models.generate_content.side_effect = ValueError("malformed JSON")
        with self.assertRaises(GenerationError):
            self.client(sdk).generate(INSTRUCTIONS, "원문")

    def test_given_structured_response_when_parsed_then_model_is_recorded(
        self,
    ):
        sdk = Mock()
        sdk.models.generate_content.return_value = self.response()
        client = self.client(sdk)
        self.assertEqual(
            client.generate(INSTRUCTIONS, "원문"),
            ("쉬운 설명입니다.", self.model),
        )
        kwargs = sdk.models.generate_content.call_args.kwargs
        self.assertEqual(kwargs["model"], self.model)
        self.assertEqual(kwargs["config"].system_instruction, INSTRUCTIONS)
        self.assertIsNone(kwargs["config"].response_schema)
        self.assertEqual(
            kwargs["config"].response_json_schema,
            ExplanationOutput.model_json_schema(),
        )
        self.assertEqual(kwargs["config"].candidate_count, 1)
        self.assertEqual(
            kwargs["config"].thinking_config.thinking_level, "MINIMAL"
        )
        client.close()
        sdk.close.assert_called_once()

    def test_given_refusal_or_failure_when_parsed_then_no_result_is_returned(
        self,
    ):
        sdk = Mock()
        client = self.client(sdk)
        for reason, parsed in (
            ("STOP", None),
            ("MAX_TOKENS", ExplanationOutput(explanation="설명입니다.")),
        ):
            sdk.models.generate_content.return_value = (
                types.GenerateContentResponse(
                    candidates=[types.Candidate(finish_reason=reason)],
                    parsed=parsed,
                )
            )
            with self.assertRaises(GenerationError):
                client.generate(INSTRUCTIONS, "원문")
        sdk.models.generate_content.return_value = (
            types.GenerateContentResponse(
                prompt_feedback=types.GenerateContentResponsePromptFeedback(
                    block_reason="SAFETY"
                ),
            )
        )
        with self.assertRaises(GenerationError):
            client.generate(INSTRUCTIONS, "원문")
        sdk.models.generate_content.side_effect = httpx.ReadTimeout(
            "mock secret"
        )
        with self.assertRaisesRegex(GenerationStopped, "네트워크"):
            client.generate(INSTRUCTIONS, "원문")

    def test_given_quota_or_auth_error_when_called_then_run_must_stop(self):
        sdk = Mock()
        cases = (
            (400, "INVALID_ARGUMENT", "INVALID_REQUEST"),
            (400, "FAILED_PRECONDITION", "INVALID_REQUEST"),
            (401, "UNAUTHENTICATED", "AUTHENTICATION_ERROR"),
            (402, None, "PAYMENT_REQUIRED"),
            (403, "PERMISSION_DENIED", "PERMISSION_DENIED"),
            (404, "NOT_FOUND", "MODEL_OR_RESOURCE_NOT_FOUND"),
            (429, "RESOURCE_EXHAUSTED", "RATE_LIMIT_EXCEEDED"),
            (500, "INTERNAL", "SERVER_ERROR"),
            (503, "UNAVAILABLE", "SERVICE_UNAVAILABLE"),
            (504, "DEADLINE_EXCEEDED", "UPSTREAM_TIMEOUT"),
            (418, None, "HTTP_ERROR"),
        )
        for code, status, error_type in cases:
            sdk.models.generate_content.side_effect = errors.APIError(
                code,
                {
                    "error": {
                        "code": code,
                        "status": status,
                        "message": "secret must not be logged",
                    },
                },
            )
            expected = QuotaExceededError if code == 429 else GenerationStopped
            with self.subTest(code=code), self.assertRaises(
                expected
            ) as raised:
                self.client(sdk).generate(INSTRUCTIONS, "원문")
            self.assertNotIn("secret", str(raised.exception))
            self.assertEqual(
                raised.exception.diagnostics(),
                {
                    "http_status": code,
                    "error_type": error_type,
                    "provider_status": status,
                },
            )

    def test_given_http_response_when_classified_then_actual_status_wins(self):
        sdk = Mock()
        sdk.models.generate_content.side_effect = errors.APIError(
            400,
            {"error": {"status": "unknown secret status"}},
            response=httpx.Response(503),
        )
        with self.assertRaises(GenerationStopped) as raised:
            self.client(sdk).generate(INSTRUCTIONS, "원문")
        self.assertEqual(raised.exception.http_status, 503)
        self.assertEqual(raised.exception.error_type, "SERVICE_UNAVAILABLE")
        self.assertIsNone(raised.exception.provider_status)
        self.assertNotIn("secret", str(raised.exception))

    def test_given_transport_failures_when_called_then_no_http_code_invented(
        self,
    ):
        sdk = Mock()
        cases = (
            (httpx.ConnectTimeout, "CONNECT_TIMEOUT"),
            (httpx.ReadTimeout, "READ_TIMEOUT"),
            (httpx.WriteTimeout, "WRITE_TIMEOUT"),
            (httpx.PoolTimeout, "POOL_TIMEOUT"),
            (httpx.ConnectError, "CONNECTION_ERROR"),
            (httpx.ProxyError, "PROXY_ERROR"),
            (httpx.ReadError, "NETWORK_ERROR"),
        )
        for kind, error_type in cases:
            sdk.models.generate_content.side_effect = kind(
                "secret URL and key"
            )
            with self.subTest(error_type=error_type):
                with self.assertRaises(GenerationStopped) as raised:
                    self.client(sdk).generate(INSTRUCTIONS, "원문")
                self.assertEqual(
                    raised.exception.diagnostics(),
                    {
                        "http_status": None,
                        "error_type": error_type,
                        "provider_status": None,
                    },
                )
                self.assertNotIn("secret", str(raised.exception))

    def test_given_httpx_status_error_when_called_then_http_status_is_kept(
        self,
    ):
        sdk = Mock()
        sdk.models.generate_content.side_effect = httpx.HTTPStatusError(
            "secret request body",
            request=httpx.Request("POST", "https://example.invalid"),
            response=httpx.Response(429),
        )
        with self.assertRaises(QuotaExceededError) as raised:
            self.client(sdk).generate(INSTRUCTIONS, "원문")
        self.assertEqual(raised.exception.http_status, 429)
        self.assertEqual(raised.exception.error_type, "RATE_LIMIT_EXCEEDED")
        self.assertNotIn("secret", str(raised.exception))

    def test_given_local_config_failure_when_checked_then_no_http_code_exists(
        self,
    ):
        sdk = Mock()
        with self.assertRaises(GenerationStopped) as raised:
            GeminiClient(self.model, sdk)
        self.assertIsNone(raised.exception.http_status)
        self.assertEqual(raised.exception.error_type, "FREE_TIER_UNCONFIRMED")
        sdk.models.generate_content.assert_not_called()

    def test_given_unconfirmed_or_paid_model_when_created_then_sdk_is_not_used(
        self,
    ):
        sdk = Mock()
        with self.assertRaises(GenerationStopped):
            GeminiClient(self.model, sdk)
        with self.assertRaises(GenerationStopped):
            GeminiClient("gemini-4-argon", sdk, free_tier_confirmed=True)
        sdk.models.generate_content.assert_not_called()

    def test_given_env_config_when_client_created_then_retries_are_disabled(
        self,
    ):
        config = {
            "GEMINI_API_KEY": "mock-key",
            "GEMINI_MODEL": self.model,
            "GEMINI_FREE_TIER_CONFIRMED": "true",
        }
        with patch.dict(os.environ, {}, clear=True), patch(
            "llm.client.dotenv_values", return_value={}
        ):
            with self.assertRaises(GenerationStopped):
                GeminiClient.from_env(Path("not-used"))
            with patch.dict(os.environ, config), patch(
                "llm.client.genai.Client"
            ) as sdk:
                client = GeminiClient.from_env()
                self.assertEqual(client.model, self.model)
                options = sdk.call_args.kwargs["http_options"]
                self.assertEqual(options.retry_options.attempts, 1)
                self.assertEqual(options.timeout, 30000)
                self.assertFalse(sdk.call_args.kwargs["vertexai"])
                self.assertFalse(sdk.call_args.kwargs["enterprise"])
                self.assertEqual(
                    options.base_url,
                    "https://generativelanguage.googleapis.com",
                )
                for key in (
                    "GEMINI_TIMEOUT_SECONDS",
                    "GEMINI_REQUEST_INTERVAL_SECONDS",
                ):
                    for value in ("nan", "inf", "0", "bad"):
                        with patch.dict(
                            os.environ, {key: value}
                        ), self.assertRaises(GenerationStopped):
                            GeminiClient.from_env()
                for key, value in (
                    ("GEMINI_FREE_TIER_CONFIRMED", "false"),
                    ("GEMINI_MODEL", "paid-model"),
                ):
                    sdk.reset_mock()
                    with patch.dict(
                        os.environ, {key: value}
                    ), self.assertRaises(GenerationStopped):
                        GeminiClient.from_env()
                    sdk.assert_not_called()

    def test_given_request_interval_when_called_twice_then_wait_is_applied(
        self,
    ):
        sdk = Mock()
        sdk.models.generate_content.return_value = self.response()
        client = self.client(sdk, interval_seconds=12)
        with patch(
            "llm.client.time.monotonic", side_effect=[100, 103, 112]
        ), patch("llm.client.time.sleep") as sleep:
            client.generate(INSTRUCTIONS, "원문")
            client.generate(INSTRUCTIONS, "원문")
        sleep.assert_called_once_with(9)


class GenerationCommandTests(unittest.TestCase):
    def test_given_sdk_error_when_cli_stops_then_only_safe_fields_are_printed(
        self,
    ):
        secret = "CANARY_API_KEY_AND_PRIVATE_DEFINITION"
        for http_error in (True, False):
            with self.subTest(http_error=http_error):
                sdk = Mock()
                if http_error:
                    sdk.models.generate_content.side_effect = errors.APIError(
                        403,
                        {
                            "error": {
                                "status": secret,
                                "message": secret,
                                "details": [{"api_key": secret}],
                            }
                        },
                    )
                else:
                    sdk.models.generate_content.side_effect = ValueError(
                        secret
                    )
                client = GeminiClient(
                    "gemini-3.5-flash-lite",
                    sdk,
                    free_tier_confirmed=True,
                    interval_seconds=0,
                )
                engine = Mock()
                stdout, stderr = io.StringIO(), io.StringIO()
                with patch(
                    "scripts.generate_glossary_explanations.create_database_engine",
                    return_value=engine,
                ), patch(
                    "scripts.generate_glossary_explanations.prepare_table",
                ), patch(
                    "scripts.generate_glossary_explanations.generate_for_term",
                    side_effect=lambda engine, term, generate: generate(
                        term, secret
                    ),
                ) as runner, patch(
                    "scripts.generate_glossary_explanations.GeminiClient.from_env",
                    return_value=client,
                ), redirect_stdout(
                    stdout
                ), redirect_stderr(
                    stderr
                ):
                    terms = ["국채", "고용률"] if http_error else ["국채"]
                    code = main(["--terms", *terms])
                self.assertEqual(code, 1)
                report = json.loads(stdout.getvalue())["results"]
                self.assertEqual(len(report), 1)
                self.assertEqual(
                    report[0]["status"], "stopped" if http_error else "failed"
                )
                self.assertEqual(
                    report[0]["http_status"], 403 if http_error else None
                )
                self.assertEqual(
                    report[0]["error_type"],
                    (
                        "PERMISSION_DENIED"
                        if http_error
                        else "RESPONSE_VALIDATION_ERROR"
                    ),
                )
                self.assertIsNone(report[0]["provider_status"])
                self.assertNotIn(secret, stdout.getvalue() + stderr.getvalue())
                runner.assert_called_once()
                engine.begin.assert_not_called()
                engine.connect.assert_not_called()
                engine.dispose.assert_called_once()
                sdk.close.assert_called_once()

    def test_given_no_explicit_scope_when_run_then_generation_is_blocked(self):
        for args in (
            [],
            ["--terms", "a", "b", "c", "d"],
            ["--all", "--terms", "a"],
        ):
            with patch(
                "scripts.generate_glossary_explanations.create_database_engine"
            ) as engine:
                with redirect_stderr(io.StringIO()), self.assertRaises(
                    SystemExit
                ) as result:
                    main(args)
                self.assertEqual(result.exception.code, 2)
                engine.assert_not_called()
