import argparse
import json
from pathlib import Path
import sys

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import func, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError

from db.database import DEFAULT_ENV, create_database_engine, validate_database_name
from db.models import GlossaryTerm


DEFAULT_JSON = Path(__file__).resolve().parents[1] / "data" / "output" / "glossary.json"
FIELDS = frozenset(column.name for column in GlossaryTerm.__table__.columns)
REVIEW_STATUSES = {"verified", "automated_checks_passed", "needs_review"}


def _text(value):
    return isinstance(value, str) and bool(value.strip()) and "\x00" not in value


def validate_records(records: list[dict], *, expected_count: int = 789) -> None:
    """원문을 정제/형변환하지 않고 필드·중복·JSON 구조를 사전 검증한다."""
    if not isinstance(records, list) or len(records) != expected_count or not records:
        raise ValueError(f"JSON은 {expected_count}개 용어의 배열이어야 합니다")
    seen = set()
    for index, record in enumerate(records, 1):
        prefix = f"{index}번째 용어: "
        if not isinstance(record, dict) or set(record) != FIELDS:
            raise ValueError(prefix + "기존 일곱 필드가 정확히 필요합니다")
        if not _text(record["term"]) or not _text(record["definition"]):
            raise ValueError(prefix + "term/definition이 비어 있거나 저장할 수 없는 문자 포함")
        if record["term"] in seen:
            raise ValueError(prefix + "중복 term")
        seen.add(record["term"])
        formulas = record["formulas"]
        if not isinstance(formulas, list) or any(
                not isinstance(item, dict) or set(item) != {"expression"} or not _text(item["expression"])
                for item in formulas):
            raise ValueError(prefix + "formulas는 expression만 가진 객체 배열이어야 합니다")
        related = record["related_terms"]
        if not isinstance(related, list) or any(not _text(term) for term in related):
            raise ValueError(prefix + "related_terms는 문자열 배열이어야 합니다")
        for field in ("source_pdf_page", "source_book_page"):
            page = record[field]
            if page is None and field == "source_book_page":
                continue
            if type(page) is not int or not 1 <= page <= 2147483647:
                raise ValueError(prefix + field + "는 양의 정수여야 합니다")
        if not isinstance(record["review_status"], str) or record["review_status"] not in REVIEW_STATUSES:
            raise ValueError(prefix + "알 수 없는 review_status")


def _unique_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("JSON 객체 안에 중복된 필드가 있습니다")
        result[key] = value
    return result


def load_glossary(path: str | Path = DEFAULT_JSON, *, expected_count: int = 789) -> list[dict]:
    records = json.loads(Path(path).read_text(encoding="utf-8"), object_pairs_hook=_unique_keys)
    validate_records(records, expected_count=expected_count)
    return records


def verify_records(connection, records: list[dict]) -> int:
    """입력 용어의 모든 필드를 비교한다. 불일치하면 커밋 전에 오류로 처리한다."""
    table = GlossaryTerm.__table__
    stored = connection.execute(select(table).where(table.c.term.in_([r["term"] for r in records]))).mappings()
    actual = {row["term"]: dict(row) for row in stored}
    if len(actual) != len(records) or any(actual.get(record["term"]) != record for record in records):
        raise ValueError("저장한 데이터가 원본 JSON과 일치하지 않습니다. 전체 적재를 롤백합니다")
    return len(actual)


def import_glossary(engine, records: list[dict], *, expected_count: int = 789,
                    testing: bool = False) -> dict:
    validate_records(records, expected_count=expected_count)
    if engine.dialect.name != "postgresql":
        raise ValueError("PostgreSQL에서만 적재할 수 있습니다")
    validate_database_name(engine.url.database, testing=testing)
    table = GlossaryTerm.__table__
    changed = 0
    # 테이블 생성, 모든 배치 upsert, 원본 대조를 단일 트랜잭션으로 처리한다.
    with engine.begin() as connection:
        current = connection.scalar(select(func.current_database()))
        validate_database_name(current, testing=testing)
        if current != engine.url.database:
            raise ValueError("접속한 DB가 설정된 DB와 다릅니다")
        table.create(connection, checkfirst=True)
        for offset in range(0, len(records), 100):
            statement = insert(table).values(records[offset:offset + 100])
            updates = {name: statement.excluded[name] for name in FIELDS if name != "term"}
            statement = statement.on_conflict_do_update(
                index_elements=[table.c.term], set_=updates,
                where=or_(*(table.c[name].is_distinct_from(value) for name, value in updates.items())))
            # 드라이버의 rowcount=-1에 의존하지 않고 실제 쓰인 행을 집계한다.
            changed += len(connection.execute(statement.returning(table.c.term)).scalars().all())
        matched = verify_records(connection, records)
        total = connection.scalar(select(func.count()).select_from(table))
    return {"source_count": len(records), "written_count": changed,
            "verified_count": matched, "total_count": total}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="금융 용어 789개를 monow_ai에 적재하고 원본과 검증")
    parser.add_argument("--input", type=Path, default=DEFAULT_JSON)
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV)
    args = parser.parse_args(argv)
    engine = None
    try:
        records = load_glossary(args.input)
        engine = create_database_engine(env_file=args.env_file)
        result = import_glossary(engine, records)
    except (OSError, ValueError) as error:
        print(f"적재 실패: {error}", file=sys.stderr)
        return 1
    except SQLAlchemyError as error:
        # 연결 문자열, 비밀번호,바인딩 값을 오류 로그로 노출하지 않는다.
        code = getattr(getattr(error, "orig", None), "sqlstate", None)
        print(f"DB 작업 실패({type(error).__name__}, SQLSTATE={code or '없음'}). "
              "접속 설정 - DB 존재 여부,권한을 확인, 이번 트랜잭션은 커밋되지 않았습니다.", file=sys.stderr)
        return 1
    finally:
        if engine is not None:
            engine.dispose()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
