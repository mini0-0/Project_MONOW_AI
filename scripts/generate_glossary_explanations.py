import argparse
from collections import Counter
import json
from pathlib import Path
import sys

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import func, inspect, select
from sqlalchemy.exc import SQLAlchemyError

from db.database import create_database_engine, validate_database_name
from db.models import (
    SIMPLE_CONSTRAINT_NAME,
    GlossaryExplanation,
    GlossaryTerm,
)
from db.repositories.glossary_explanations import GlossaryExplanationRepository
from llm.client import GeminiClient, GenerationError, GenerationStopped
from llm.glossary_generator import (
    definition_hash,
    generate_explanation,
    validate_explanation,
)


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
        schema_map = connection.get_execution_options().get(
            "schema_translate_map", {}
        )
        schema = schema_map.get(table.schema, table.schema)
        constraints = inspect(connection).get_check_constraints(
            table.name, schema=schema
        )
        names = {item["name"] for item in constraints}

        if (
            SIMPLE_CONSTRAINT_NAME not in names
            or "simple_explanation_format" in names
        ):
            raise ValueError(
                "기존 설명 테이블의 30자 검증 제약조건이 없습니다. "
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
            if existing["definition_hash"] != definition_hash(
                source["definition"]
            ):
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
            {"term": term, "explanation_type": "SIMPLE", **record}
        )

    return {"term": term, "status": "created" if saved else "skipped_existing"}


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="미생성 SIMPLE 설명을 확인된 Gemini 무료 등급에서만 생성합니다."
    )
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument(
        "--terms", nargs="+", help="정식 용어 이름, 한 번에 최대 3개"
    )
    selection.add_argument(
        "--all",
        action="store_true",
        help="needs_review 제외 전체 용어를 명시적으로 생성",
    )
    args = parser.parse_args(argv)

    if args.terms and len(args.terms) > 3:
        parser.error(
            "--terms는 최대 3개입니다. 전체 생성은 --all로 명시하세요"
        )

    engine, client = None, None
    results = []

    def generate(term, definition):
        nonlocal client

        if client is None:
            client = GeminiClient.from_env()

        return generate_explanation(client, term, definition)

    try:
        engine = create_database_engine()
        prepare_table(engine)

        if args.all:
            with engine.connect() as connection:
                statement = (
                    select(GlossaryTerm.term)
                    .where(GlossaryTerm.review_status != "needs_review")
                    .order_by(GlossaryTerm.term)
                )
                terms = list(connection.scalars(statement))
        else:
            terms = list(dict.fromkeys(args.terms))

        for term in terms:
            try:
                results.append(generate_for_term(engine, term, generate))
            except GenerationStopped as error:
                results.append(
                    {
                        "term": term,
                        "status": "stopped",
                        "reason": str(error),
                        **error.diagnostics(),
                    }
                )
                break
            except GenerationError as error:
                results.append(
                    {
                        "term": term,
                        "status": "failed",
                        "reason": str(error),
                        **error.diagnostics(),
                    }
                )
            except SQLAlchemyError:
                results.append(
                    {
                        "term": term,
                        "status": "failed",
                        "reason": "DB 저장 실패, 해당 용어 롤백",
                    }
                )
    except ValueError as error:
        print(f"실행 준비 실패: {error}", file=sys.stderr)
        return 1

    except SQLAlchemyError:
        print(
            "DB 설정 또는 설명 테이블 준비 실패. 접속 정보와 권한을 확인하세요.",
            file=sys.stderr,
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
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return int(
        any(
            item["status"]
            in (
                "failed",
                "not_found",
                "skipped_stale",
                "skipped_invalid",
                "stopped",
            )
            for item in results
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
