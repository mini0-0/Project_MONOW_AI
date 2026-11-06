from datetime import datetime
import re
import unicodedata

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

SIMPLE_EXPLANATION_LIMIT = 60
SIMPLE_CONSTRAINT_NAME = "simple_explanation_format_v3"
# POSIX punct는 +, ₩ 같은 기호도 제외할 수 있어 Python과 SQL에 동일한 집합을 사용한다.
SIMPLE_IGNORED_PATTERN = (
    "["
    + re.escape(
        "".join(
            char
            for codepoint in range(0x110000)
            if (char := chr(codepoint)).isspace() or unicodedata.category(char).startswith("P")
        )
    )
    + "]"
)
_SQL_IGNORED_PATTERN = SIMPLE_IGNORED_PATTERN.replace("\\", "\\\\").replace("'", "''")


def simple_explanation_length(text: str) -> int:
    return len(re.sub(SIMPLE_IGNORED_PATTERN, "", text))


def validate_simple_explanation(text: str) -> str:
    if not isinstance(text, str):
        raise ValueError("설명은 문자열이어야 합니다")

    if any(unicodedata.category(char).startswith("C") or char in "\u2028\u2029" for char in text):
        raise ValueError("설명에 개행이나 제어문자가 있습니다")

    text = text.strip()
    if not 1 <= simple_explanation_length(text) <= SIMPLE_EXPLANATION_LIMIT:
        raise ValueError("설명은 공백과 문장부호 제외 1~60자여야 합니다")

    without_decimals = re.sub(r"(?<=[0-9])\.(?=[0-9])", "", text)
    if re.fullmatch(r"[^.!?。！？]+\.", without_decimals) is None or text.startswith(
        ("#", "- ", "* ", "```", '"', "'", "“", "‘")
    ):
        raise ValueError("설명은 마지막 마침표가 있는 완결된 한 문장이어야 합니다")

    return text


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
        CheckConstraint(
            "source_book_page IS NULL OR source_book_page > 0", name="book_page_positive"
        ),
        CheckConstraint(
            "review_status IN " "('verified', 'automated_checks_passed', 'needs_review')",
            name="review_status_allowed",
        ),
        {"schema": "public"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    term: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    definition: Mapped[str] = mapped_column(Text, nullable=False)
    formulas: Mapped[list[dict]] = mapped_column(JSONB, nullable=False)
    related_terms: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    source_pdf_page: Mapped[int] = mapped_column(Integer, nullable=False)
    source_book_page: Mapped[int | None] = mapped_column(Integer, nullable=True)
    review_status: Mapped[str] = mapped_column(Text, nullable=False)


class GlossaryExplanation(Base):
    __tablename__ = "glossary_explanations"
    __table_args__ = (
        UniqueConstraint("term_id", "explanation_type"),
        CheckConstraint(
            "explanation_type IN ('SIMPLE', 'DETAILED')", name="explanation_type_allowed"
        ),
        CheckConstraint("length(btrim(explanation)) > 0", name="explanation_not_empty"),
        CheckConstraint(
            "explanation_type <> 'SIMPLE' OR ("
            "char_length(regexp_replace("
            f"explanation, E'{_SQL_IGNORED_PATTERN}', '', 'g')) "
            f"BETWEEN 1 AND {SIMPLE_EXPLANATION_LIMIT} "
            "AND explanation !~ '[[:cntrl:]]' "
            "AND regexp_replace(explanation, "
            "'(?<=[0-9])[.](?=[0-9])', '', 'g') "
            "~ '^[^.!?。！？]+[.]$')",
            name=SIMPLE_CONSTRAINT_NAME,
        ),
        CheckConstraint(
            "length(btrim(model)) > 0 AND length(btrim(prompt_version)) > 0",
            name="generation_metadata_not_empty",
        ),
        CheckConstraint("definition_hash ~ '^[0-9a-f]{64}$'", name="definition_hash_format"),
        {"schema": "public"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    term_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("public.glossary_terms.id"), nullable=False
    )
    explanation_type: Mapped[str] = mapped_column(Text, nullable=False)
    explanation: Mapped[str] = mapped_column(Text, nullable=False)
    model: Mapped[str] = mapped_column(Text, nullable=False)
    prompt_version: Mapped[str] = mapped_column(Text, nullable=False)
    definition_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
