from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, StringConstraints, field_validator

from db.models import validate_simple_explanation


class SimpleGlossaryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    term: Annotated[
        str,
        StringConstraints(
            strict=True,
            strip_whitespace=True,
            min_length=1,
            max_length=200,
            pattern=r"^[^\x00-\x1f\x7f]+$",
        ),
    ]


class SimpleGlossaryResponse(BaseModel):
    term: str
    summary: str
    review_status: Literal[
        "verified", "automated_checks_passed", "needs_review"
    ]
    source_pdf_page: int
    source_book_page: int | None

    @field_validator("summary")
    @classmethod
    def validate_summary(cls, value: str) -> str:
        return validate_simple_explanation(value)
