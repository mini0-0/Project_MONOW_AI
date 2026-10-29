import os
from pathlib import Path

from dotenv import dotenv_values
from sqlalchemy import create_engine
from sqlalchemy.engine import URL, Engine, make_url


DEFAULT_ENV = Path(__file__).resolve().parents[1] / ".env"


def validate_database_name(name: str | None, *, testing: bool = False) -> None:
    # 기존 MONOW DB나 개발 DB를 테스트 대상으로 사용하는 것을 차단
    if testing:
        allowed = name == "monow_ai_test" or bool(name and name.startswith("monow_ai_test_"))
    else:
        allowed = name == "monow_ai"
    if not allowed:
        raise ValueError("DB 이름은 monow_ai여야 합니다" if not testing else
                         "테스트 DB 이름은 monow_ai_test 또는 monow_ai_test_*여야 합니다")


def database_url(env_file: str | Path = DEFAULT_ENV) -> URL:
    # OS 환경변수가 .env보다 우선하며, 전역 환경변수는 변경하지 않음
    config = {**dotenv_values(env_file), **os.environ}
    name = config.get("DB_NAME", "monow_ai")
    validate_database_name(name)
    required = ("DB_HOST", "DB_USER", "DB_PASSWORD")
    missing = [key for key in required if not config.get(key)]
    if missing:
        raise ValueError("DB 설정이 비어 있습니다: " + ", ".join(missing))
    try:
        port = int(config.get("DB_PORT", "5432"))
        if not 1 <= port <= 65535:
            raise ValueError
    except (TypeError, ValueError):
        raise ValueError("DB_PORT는 1~65535 정수여야 합니다") from None
    # 비밀번호에 @, :, / 등이 있어도 URL을 문자열로 조합하지 않아 손상되지 않음
    return URL.create("postgresql+psycopg", username=config["DB_USER"],
                      password=config["DB_PASSWORD"], host=config["DB_HOST"],
                      port=port, database=name)


def create_database_engine(url: URL | str | None = None, *, env_file=DEFAULT_ENV,
                           testing: bool = False) -> Engine:
    resolved = make_url(url) if url is not None else database_url(env_file)
    if resolved.drivername != "postgresql+psycopg":
        raise ValueError("postgresql+psycopg 연결만 지원합니다")
    validate_database_name(resolved.database, testing=testing)
    return create_engine(resolved, pool_pre_ping=True, hide_parameters=True,
                         connect_args={"connect_timeout": 5})
