from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert

from db.models import GlossaryExplanation, GlossaryTerm


class GlossaryExplanationRepository:
    def __init__(self, connection):
        self.connection = connection

    def find(self, term: str, explanation_type: str = "SIMPLE") -> dict | None:
        table = GlossaryExplanation.__table__
        row = (
            self.connection.execute(
                select(table).where(
                    table.c.term == term,
                    table.c.explanation_type == explanation_type,
                )
            )
            .mappings()
            .one_or_none()
        )
        return dict(row) if row is not None else None

    def try_generation_lock(
        self, term: str, explanation_type: str = "SIMPLE"
    ) -> bool:
        key = f"monow_ai:glossary_explanations:{term}:{explanation_type}"
        return self.connection.scalar(
            select(
                func.pg_try_advisory_xact_lock(func.hashtextextended(key, 0))
            )
        )

    def source_for_generation(self, term: str) -> dict | None:
        # 생성 중 원본이 변경되지 않도록 공유 행 잠금을 유지한다. 원본에는 쓰지 않는다.
        table = GlossaryTerm.__table__
        row = (
            self.connection.execute(
                select(table)
                .where(table.c.term == term)
                .with_for_update(read=True)
            )
            .mappings()
            .one_or_none()
        )
        return dict(row) if row is not None else None

    def save_if_absent(self, record: dict) -> bool:
        table = GlossaryExplanation.__table__
        statement = (
            insert(table)
            .values(record)
            .on_conflict_do_nothing(
                index_elements=[table.c.term, table.c.explanation_type]
            )
            .returning(table.c.term)
        )
        return (
            self.connection.execute(statement).scalar_one_or_none() is not None
        )
