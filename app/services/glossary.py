import hashlib
import re

from pydantic import ValidationError

from app.schemas.glossary import SimpleGlossaryResponse
from db.repositories.glossary import GlossaryRepository


class TermNotFoundError(LookupError):
    pass


class SummaryUnavailableError(ValueError):
    pass


class ExplanationNotFoundError(SummaryUnavailableError):
    pass


class StaleExplanationError(SummaryUnavailableError):
    pass


# def first_sentence(definition: str) -> str:
#     text = re.sub(r"\s+", " ", definition).strip()
#     if not text:
#         raise SummaryUnavailableError("설명 본문이 비어 있습니다")
#     pairs = {"(": ")", "[": "]", "{": "}"}
#     stack = []
#     for index, char in enumerate(text):
#         if char in pairs:
#             stack.append(pairs[char])
#         elif stack and char == stack[-1]:
#             stack.pop()
#         # 괄호 속 영문 약어와 소수점을 문장 끝으로 오인하지 않는다.
#         if (
#             not stack
#             and char in ".!?。！？"
#             and (index + 1 == len(text) or text[index + 1].isspace())
#         ):
#             return text[: index + 1]
#     # 완결 문장이 없으면 임의 글자 수로 자르지 않고 원문을 한 줄로 반환한다.
#     return text


def simple_glossary(repository: GlossaryRepository, term: str) -> SimpleGlossaryResponse:
    record = repository.find_by_term(term)

    if record is None:
        raise TermNotFoundError(term)

    explanation = repository.find_simple_explanation(term)

    if explanation is None:
        raise ExplanationNotFoundError(term)

    current_hash = hashlib.sha256(record["definition"].encode("utf-8")).hexdigest()

    if explanation["definition_hash"] != current_hash:
        raise StaleExplanationError(term)

    try:
        return SimpleGlossaryResponse(
            term=record["term"],
            summary=explanation["explanation"],
            review_status=record["review_status"],
            source_pdf_page=record["source_pdf_page"],
            source_book_page=record["source_book_page"],
        )
    except ValidationError:
        raise SummaryUnavailableError("저장된 설명의 형식 검토가 필요합니다") from None
