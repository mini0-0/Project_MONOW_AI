from sqlalchemy import select
from sqlalchemy.engine import Connection

from db.models import GlossaryTerm
from db.repositories.glossary_explanations import GlossaryExplanationRepository


class GlossaryRepository:
    def __init__(self, connection: Connection):
        self.connection = connection

    def find_by_term(self, term: str) -> dict | None:
        table = GlossaryTerm.__table__
        row = self.connection.execute(select(table).where(table.c.term == term)).mappings().one_or_none()
        return dict(row) if row is not None else None

    def find_simple_explanation(self, term: str) -> dict | None:
        return GlossaryExplanationRepository(self.connection).find(term, "SIMPLE")
