import math
import os
import time

from dotenv import dotenv_values
from google import genai
from google.genai import errors, types
import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator

from db.database import DEFAULT_ENV
from db.models import validate_simple_explanation

FREE_TIER_MODELS = frozenset({"gemini-3.5-flash-lite", "gemini-3.5-flash"})
MAX_HTTP_RETRIES = 3
RETRY_BACKOFF_SECONDS = 2
HTTP_ERROR_TYPES = {
    400: "INVALID_REQUEST",
    401: "AUTHENTICATION_ERROR",
    402: "PAYMENT_REQUIRED",
    403: "PERMISSION_DENIED",
    404: "MODEL_OR_RESOURCE_NOT_FOUND",
    408: "HTTP_TIMEOUT",
    409: "CONFLICT",
    422: "INVALID_REQUEST",
    429: "RATE_LIMIT_EXCEEDED",
    500: "SERVER_ERROR",
    501: "UNSUPPORTED_OPERATION",
    502: "BAD_GATEWAY",
    503: "SERVICE_UNAVAILABLE",
    504: "UPSTREAM_TIMEOUT",
}
PROVIDER_STATUSES = frozenset(
    {
        "INVALID_ARGUMENT",
        "FAILED_PRECONDITION",
        "UNAUTHENTICATED",
        "PERMISSION_DENIED",
        "NOT_FOUND",
        "RESOURCE_EXHAUSTED",
        "INTERNAL",
        "UNAVAILABLE",
        "DEADLINE_EXCEEDED",
        "UNKNOWN",
        "CANCELLED",
        "OUT_OF_RANGE",
        "ALREADY_EXISTS",
        "ABORTED",
        "UNIMPLEMENTED",
        "DATA_LOSS",
    }
)


class GenerationError(ValueError):
    default_error_type = "GENERATION_ERROR"

    def __init__(self, message, *, http_status=None, error_type=None, provider_status=None):
        super().__init__(message)
        self.http_status = http_status
        self.error_type = error_type or self.default_error_type
        # SDK의 원문 상태나 메시지를 그대로 로그에 전달하지 않는다.
        self.provider_status = (
            provider_status
            if isinstance(provider_status, str) and provider_status in PROVIDER_STATUSES
            else None
        )

    def diagnostics(self):
        return {
            "http_status": self.http_status,
            "error_type": self.error_type,
            "provider_status": self.provider_status,
        }


class GenerationStopped(GenerationError):
    default_error_type = "GENERATION_STOPPED"


class QuotaExceededError(GenerationStopped):
    default_error_type = "RATE_LIMIT_EXCEEDED"


def _http_diagnostics(error):
    status = getattr(getattr(error, "response", None), "status_code", None)

    if type(status) is not int or not 100 <= status <= 599:
        status = getattr(error, "code", None)

    if type(status) is not int or not 100 <= status <= 599:
        status = None
    error_type = HTTP_ERROR_TYPES.get(status, "HTTP_ERROR")
    message = getattr(error, "message", None)
    # 원문은 분석에만 사용하며 메시지에 포함된 값이나 경로는 출력하지 않는다.

    if status == 400 and isinstance(message, str):
        message = message.lower()
        if (
            any(word in message for word in ("unknown name", "unknown field"))
            and any(word in message for word in ("response_schema", "responseschema"))
            and any(
                word in message
                for word in (
                    "additional_properties",
                    "additionalproperties",
                    "min_length",
                    "minlength",
                )
            )
        ):
            error_type = "UNSUPPORTED_RESPONSE_SCHEMA_FIELD"

    return {
        "http_status": status,
        "error_type": error_type,
        "provider_status": getattr(error, "status", None),
    }


class ExplanationOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    explanation: str = Field(strict=True, min_length=1)

    @field_validator("explanation")
    @classmethod
    def validate_text(cls, value: str) -> str:
        return validate_simple_explanation(value)


class GeminiClient:
    def __init__(
        self, model: str, sdk: genai.Client, *, free_tier_confirmed=False, interval_seconds=12
    ):
        self._check_free_tier(model, free_tier_confirmed)
        self.model = model
        self.sdk = sdk
        self.interval_seconds = interval_seconds
        self._last_request = None

    @staticmethod
    def _check_free_tier(model, confirmed):
        if not confirmed:
            raise GenerationStopped(
                "해당 API Key 프로젝트의 무료 등급 확인이 필요합니다. "
                "AI Studio에서 Free 확인 후 GEMINI_FREE_TIER_CONFIRMED=true를 설정하세요",
                error_type="FREE_TIER_UNCONFIRMED",
            )
        if model not in FREE_TIER_MODELS:
            raise GenerationStopped(
                "무료 등급 지원을 확인한 Gemini 모델만 허용합니다", error_type="MODEL_NOT_ALLOWED"
            )

    @classmethod
    def from_env(cls, env_file=DEFAULT_ENV):
        config = {**dotenv_values(env_file), **os.environ}

        if not all(
            config.get(key) and config[key].strip() for key in ("GEMINI_API_KEY", "GEMINI_MODEL")
        ):
            raise GenerationStopped(
                "GEMINI_API_KEY와 GEMINI_MODEL 설정이 필요합니다", error_type="CONFIG_MISSING"
            )

        model = config["GEMINI_MODEL"].strip()
        confirmed = config.get("GEMINI_FREE_TIER_CONFIRMED", "").lower() == "true"
        cls._check_free_tier(model, confirmed)

        try:
            timeout = float(config.get("GEMINI_TIMEOUT_SECONDS", "60"))
            interval = float(config.get("GEMINI_REQUEST_INTERVAL_SECONDS", "12"))
            if (
                not math.isfinite(timeout)
                or timeout < 0.001
                or not math.isfinite(interval)
                or interval <= 0
            ):
                raise ValueError
        except (ValueError, TypeError):
            raise GenerationStopped(
                "Gemini 타임아웃은 0.001초 이상, 요청 간격은 양수여야 합니다",
                error_type="CONFIG_INVALID",
            ) from None

        # 모델만으로 프로젝트의 결제 등급을 판별할 수 없어 수동 확인을 필수로 한다.
        # Developer API로 고정하고 환경변수에 의한 Vertex/Enterprise 전환을 막는다.
        sdk = genai.Client(
            api_key=config["GEMINI_API_KEY"],
            vertexai=False,
            enterprise=False,
            http_options=types.HttpOptions(
                base_url="https://generativelanguage.googleapis.com",
                api_version="v1beta",
                timeout=int(timeout * 1000),
                retry_options=types.HttpRetryOptions(attempts=1),
            ),
        )
        return cls(model, sdk, free_tier_confirmed=True, interval_seconds=interval)

    def _generate_content(self, instructions: str, source: str):
        for retry in range(MAX_HTTP_RETRIES + 1):
            delay = RETRY_BACKOFF_SECONDS * (2 ** (retry - 1)) if retry else 0
            if self._last_request is not None:
                delay = max(delay, self.interval_seconds - (time.monotonic() - self._last_request))
            # 백오프 중에도 기존 요청 간격을 지켜 무료 등급 호출 속도를 유지한다.
            if delay > 0:
                time.sleep(delay)
            self._last_request = time.monotonic()

            try:
                return self.sdk.models.generate_content(
                    model=self.model,
                    contents=source,
                    config=types.GenerateContentConfig(
                        system_instruction=instructions,
                        response_mime_type="application/json",
                        # JSON Schema를 기존 OpenAPI Schema로 변환하지 않고 전송한다.
                        response_json_schema=(ExplanationOutput.model_json_schema()),
                        max_output_tokens=1024,
                        candidate_count=1,
                        thinking_config=types.ThinkingConfig(thinking_level="minimal"),
                    ),
                )
            except (errors.APIError, httpx.HTTPStatusError) as error:
                status = _http_diagnostics(error)["http_status"]
                if status not in (503, 504) or retry == MAX_HTTP_RETRIES:
                    raise

    def generate(self, instructions: str, source: str) -> tuple[str, str]:
        try:
            response = self._generate_content(instructions, source)
        except (errors.APIError, httpx.HTTPStatusError) as error:
            details = _http_diagnostics(error)
            if details["http_status"] == 429:
                raise QuotaExceededError(
                    "Gemini 요청 한도 초과(HTTP 429). "
                    "추가 호출을 중단합니다. 한도 회복 후 재실행하세요",
                    **details,
                ) from None
            raise GenerationStopped(
                f"Gemini API 오류(HTTP {details['http_status']}, "
                f"{details['error_type']})로 추가 호출을 중단합니다",
                **details,
            ) from None
        except httpx.TimeoutException as error:
            timeout_types = {
                httpx.ConnectTimeout: "CONNECT_TIMEOUT",
                httpx.ReadTimeout: "READ_TIMEOUT",
                httpx.WriteTimeout: "WRITE_TIMEOUT",
                httpx.PoolTimeout: "POOL_TIMEOUT",
            }
            error_type = next(
                (name for kind, name in timeout_types.items() if isinstance(error, kind)),
                "NETWORK_TIMEOUT",
            )
            raise GenerationStopped(
                "Gemini 네트워크 타임아웃으로 추가 호출을 중단합니다", error_type=error_type
            ) from None
        except httpx.ConnectError:
            raise GenerationStopped(
                "Gemini 네트워크 연결 실패로 추가 호출을 중단합니다", error_type="CONNECTION_ERROR"
            ) from None
        except httpx.ProxyError:
            raise GenerationStopped(
                "Gemini 프록시 연결 실패로 추가 호출을 중단합니다", error_type="PROXY_ERROR"
            ) from None
        except httpx.HTTPError:
            raise GenerationStopped(
                "Gemini 네트워크 오류로 추가 호출을 중단합니다", error_type="NETWORK_ERROR"
            ) from None
        except ValueError:
            raise GenerationError(
                "Gemini 구조화 응답 검증 실패", error_type="RESPONSE_VALIDATION_ERROR"
            ) from None

        feedback = response.prompt_feedback

        if feedback and feedback.block_reason not in (
            None,
            types.BlockedReason.BLOCKED_REASON_UNSPECIFIED,
        ):
            raise GenerationError("Gemini가 입력을 차단했습니다", error_type="RESPONSE_BLOCKED")
        if (
            not response.candidates
            or len(response.candidates) != 1
            or response.candidates[0].finish_reason != types.FinishReason.STOP
            or response.parsed is None
        ):
            raise GenerationError(
                "Gemini 응답이 완료되지 않았거나 검증에 실패했습니다",
                error_type="RESPONSE_INVALID_OR_INCOMPLETE",
            )
        if not isinstance(response.model_version, str) or not response.model_version.strip():
            raise GenerationError(
                "응답 모델 정보를 확인할 수 없습니다", error_type="RESPONSE_MODEL_MISSING"
            )

        try:
            parsed = ExplanationOutput.model_validate(response.parsed)
        except ValueError:
            raise GenerationError(
                "Gemini 구조화 응답 검증 실패", error_type="RESPONSE_VALIDATION_ERROR"
            ) from None

        return parsed.explanation, response.model_version

    def close(self):
        self.sdk.close()
