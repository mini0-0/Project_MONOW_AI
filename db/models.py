from sqlalchemy import CheckConstraint, Integer, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class GlossaryTerm(Base):
    __tablename__ = "glossary_terms"
    __table_args__ = (
        CheckConstraint("length(btrim(term)) > 0", name="term_not_empty"),
        CheckConstraint("length(btrim(definition)) > 0", name="definition_not_empty"),
        CheckConstraint("jsonb_typeof(formulas) = 'array'", name="formulas_array"),
        CheckConstraint("jsonb_typeof(related_terms) = 'array'", name="related_terms_array"),
        CheckConstraint("source_pdf_page > 0", name="pdf_page_positive"),
        CheckConstraint("source_book_page IS NULL OR source_book_page > 0", name="book_page_positive"),
        CheckConstraint("review_status IN ('verified', 'automated_checks_passed', 'needs_review')",
                        name="review_status_allowed"),
        {"schema": "public"},
    )

    term: Mapped[str] = mapped_column(Text, primary_key=True)
    definition: Mapped[str] = mapped_column(Text, nullable=False)
    formulas: Mapped[list[dict]] = mapped_column(JSONB, nullable=False)
    related_terms: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    source_pdf_page: Mapped[int] = mapped_column(Integer, nullable=False)
    source_book_page: Mapped[int | None] = mapped_column(Integer, nullable=True)
    review_status: Mapped[str] = mapped_column(Text, nullable=False)
