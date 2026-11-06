import argparse
from collections import Counter
import json
import logging
from pathlib import Path
import sys

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import func, inspect, select
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.schema import AddConstraint

from db.database import create_database_engine, validate_database_name
from db.models import SIMPLE_CONSTRAINT_NAME, GlossaryExplanation
from db.repositories.glossary_explanations import GlossaryExplanationRepository
from llm.client import GeminiClient, GenerationError, GenerationStopped
from llm.glossary_generator import definition_hash, generate_explanation, validate_explanation

logger = logging.getLogger(__name__)


def simple_constraint_sql():
    table = GlossaryExplanation.__table__
    constraint = next(item for item in table.constraints if item.name == SIMPLE_CONSTRAINT_NAME)
    statements = ["BEGIN;"]
    # 이전 제약조건을 남겨두면 새 60자 규칙보다 먼저 저장을 거부할 수 있다.
    for name in (
        "simple_explanation_format",
        "simple_explanation_format_v2",
        SIMPLE_CONSTRAINT_NAME,
    ):
        statements.append(
            "ALTER TABLE public.glossary_explanations " f"DROP CONSTRAINT IF EXISTS {name};"
        )
    statements.append(str(AddConstraint(constraint).compile(dialect=postgresql.dialect())) + ";")
    statements.append("COMMIT;")
    return "\n".join(statements)


def check_coverage(engine, *, testing=False):
    with engine.connect() as connection:
        _check_database(engine, connection, testing)

        return GlossaryExplanationRepository(connection).coverage()


def log_failure(term, stage, *, error=None):
    details = {"term": term, "stage": stage}

    if error is not None:
        details.update(error.diagnostics())
    # 예외 원문과 SQL 매개변수에는 인증 정보나 요청 본문이 포함될 수 있다.
    logger.error(json.dumps(details, ensure_ascii=False))


def _check_database(engine, connection, testing):
    validate_database_name(engine.url.database, testing=testing)
    current = connection.scalar(select(func.current_database()))
    validate_database_name(current, testing=testing)

    if current != engine.url.database:
        raise ValueError("접속한 DB가 설정과 다릅니다")


def prepare_table(engine, *, testing=False):
    with engine.begin() as connection:
        _check_database(engine, connection, testing)
        # 기존 원본 테이블을 생성하거나 변경하지 않는다.
        table = GlossaryExplanation.__table__
        table.create(connection, checkfirst=True)
        schema_map = connection.get_execution_options().get("schema_translate_map", {})
        schema = schema_map.get(table.schema, table.schema)
        constraints = inspect(connection).get_check_constraints(table.name, schema=schema)
        names = {item["name"] for item in constraints}

        if SIMPLE_CONSTRAINT_NAME not in names or names.intersection(
            {"simple_explanation_format", "simple_explanation_format_v2"}
        ):
            raise ValueError(
                "기존 설명 테이블의 60자 검증 제약조건이 없습니다. "
                "데이터를 보존하는 수동 스키마 변경이 필요합니다"
            )


def generate_for_term(engine, term, generate, *, testing=False):
    # 동시 유료 호출 방지를 위해 용어별 잠금을 생성 완료와 커밋까지 유지한다.
    with engine.begin() as connection:
        _check_database(engine, connection, testing)
        repository = GlossaryExplanationRepository(connection)

        if not repository.try_generation_lock(term):
            return {"term": term, "status": "skipped_in_progress"}

        source = repository.source_for_generation(term)

        if source is None:
            return {"term": term, "status": "not_found"}

        if source["review_status"] == "needs_review":
            return {"term": term, "status": "skipped_needs_review"}

        existing = repository.find(term)

        if existing:
            if existing["definition_hash"] != definition_hash(source["definition"]):
                return {"term": term, "status": "skipped_stale"}

            try:
                validate_explanation(existing["explanation"])
                status = "skipped_existing"
            except GenerationError:
                status = "skipped_invalid"

            return {"term": term, "status": status}

        record = generate(term, source["definition"])
        record["explanation"] = validate_explanation(record["explanation"])

        if record["definition_hash"] != definition_hash(source["definition"]):
            raise GenerationError("생성에 사용한 원문 해시가 다릅니다")

        saved = repository.save_if_absent(
            {"term_id": source["id"], "explanation_type": "SIMPLE", **record}
        )

    return {"term": term, "status": "created" if saved else "skipped_existing"}


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="미생성 SIMPLE 설명을 확인된 Gemini 무료 등급에서만 생성합니다."
    )
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--terms", nargs="+", help="정식 용어 이름, 한 번에 최대 3개")
    selection.add_argument(
        "--all", action="store_true", help="needs_review 제외 SIMPLE 설명이 없는 용어만 생성"
    )
    selection.add_argument(
        "--check-only",
        action="store_true",
        help="LLM 호출과 테이블 변경 없이 전체 SIMPLE 설명 생성 현황 조회",
    )
    selection.add_argument(
        "--print-simple-constraint-sql",
        action="store_true",
        help="DB 접속 없이 60자 CHECK 제약조건 변경 SQL만 출력",
    )
    args = parser.parse_args(argv)

    if args.print_simple_constraint_sql:
        print(simple_constraint_sql())
        return 0

    if args.terms and len(args.terms) > 3:
        parser.error("--terms는 최대 3개입니다. 전체 생성은 --all로 명시하세요")

    engine, client = None, None
    results = []

    def generate(term, definition):
        nonlocal client

        if client is None:
            client = GeminiClient.from_env()

        return generate_explanation(client, term, definition)

    try:
        engine = create_database_engine()
        if args.check_only:
            coverage = check_coverage(engine)
            print(json.dumps({"coverage": coverage}, ensure_ascii=False, indent=2))
            return int(not coverage["complete"])

        prepare_table(engine)

        if args.all:
            with engine.connect() as connection:
                _check_database(engine, connection, False)
                repository = GlossaryExplanationRepository(connection)
                terms = repository.missing_terms()
        else:
            terms = list(dict.fromkeys(args.terms))

        for term in terms:
            try:
                results.append(generate_for_term(engine, term, generate))
            except GenerationStopped as error:
                log_failure(term, "llm_stopped", error=error)
                results.append(
                    {"term": term, "status": "stopped", "reason": str(error), **error.diagnostics()}
                )
                break
            except GenerationError as error:
                log_failure(term, "llm_validation", error=error)
                results.append(
                    {"term": term, "status": "failed", "reason": str(error), **error.diagnostics()}
                )
            except SQLAlchemyError:
                log_failure(term, "db_save")
                results.append(
                    {
                        "term": term,
                        "status": "failed",
                        "reason": "DB 저장 실패, 해당 용어 롤백",
                        "error_type": "DB_SAVE_ERROR",
                    }
                )
        coverage = check_coverage(engine) if args.all else None
    except ValueError as error:
        print(f"실행 준비 실패: {error}", file=sys.stderr)
        return 1

    except SQLAlchemyError:
        print(
            "DB 연결, 조회 또는 테이블 준비 실패. 접속 정보와 권한을 확인하세요.", file=sys.stderr
        )
        return 1

    finally:
        if client is not None:
            client.close()
        if engine is not None:
            engine.dispose()

    print(
        json.dumps(
            {
                "counts": dict(Counter(r["status"] for r in results)),
                "results": results,
                "coverage": coverage,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return int(
        (args.all and not coverage["complete"])
        or any(
            item["status"] in ("failed", "not_found", "skipped_stale", "skipped_invalid", "stopped")
            for item in results
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
