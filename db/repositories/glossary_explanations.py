from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert

from db.models import GlossaryExplanation, GlossaryTerm


class GlossaryExplanationRepository:
    def __init__(self, connection):
        self.connection = connection

    def missing_terms(self) -> list[str]:
        source = GlossaryTerm.__table__
        saved = GlossaryExplanation.__table__
        exists = (
            select(saved.c.id)
            .where(saved.c.term_id == source.c.id, saved.c.explanation_type == "SIMPLE")
            .exists()
        )

        return list(
            self.connection.scalars(
                select(source.c.term)
                .where(~exists, source.c.review_status != "needs_review")
                .order_by(source.c.term)
            )
        )

    def coverage(self) -> dict:
        source = GlossaryTerm.__table__
        saved = GlossaryExplanation.__table__
        rows = (
            self.connection.execute(
                select(source.c.term, source.c.review_status, saved.c.id.label("saved_id"))
                .select_from(
                    source.outerjoin(
                        saved,
                        (saved.c.term_id == source.c.id) & (saved.c.explanation_type == "SIMPLE"),
                    )
                )
                .order_by(source.c.term)
            )
            .mappings()
            .all()
        )
        missing = [row for row in rows if row["saved_id"] is None]

        return {
            "total_terms": len(rows),
            "simple_explanations": len(rows) - len(missing),
            "missing_count": len(missing),
            "missing_terms": [row["term"] for row in missing],
            "blocked_needs_review": [
                row["term"] for row in missing if row["review_status"] == "needs_review"
            ],
            "complete": not missing,
        }

    def find(self, term: str, explanation_type: str = "SIMPLE") -> dict | None:
        table = GlossaryExplanation.__table__
        source = GlossaryTerm.__table__
        row = (
            self.connection.execute(
                select(table)
                .join(source, table.c.term_id == source.c.id)
                .where(source.c.term == term, table.c.explanation_type == explanation_type)
            )
            .mappings()
            .one_or_none()
        )

        return dict(row) if row is not None else None

    def try_generation_lock(self, term: str, explanation_type: str = "SIMPLE") -> bool:
        key = f"monow_ai:glossary_explanations:{term}:{explanation_type}"

        return self.connection.scalar(
            select(func.pg_try_advisory_xact_lock(func.hashtextextended(key, 0)))
        )

    def source_for_generation(self, term: str) -> dict | None:
        # 생성 중 원본이 변경되지 않도록 공유 행 잠금을 유지한다. 원본에는 쓰지 않는다.
        table = GlossaryTerm.__table__
        row = (
            self.connection.execute(
                select(table).where(table.c.term == term).with_for_update(read=True)
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
            .on_conflict_do_nothing(index_elements=[table.c.term_id, table.c.explanation_type])
            .returning(table.c.id)
        )

        return self.connection.execute(statement).scalar_one_or_none() is not None
