from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query

from app.api.dependencies import get_glossary_repository
from app.schemas.glossary import SimpleGlossaryRequest, SimpleGlossaryResponse
from app.services.glossary import (
    ExplanationNotFoundError,
    StaleExplanationError,
    SummaryUnavailableError,
    TermNotFoundError,
    simple_glossary,
)
from db.repositories.glossary import GlossaryRepository

router = APIRouter(prefix="/api/v1/ai/glossary", tags=["glossary"])


@router.get(
    "/simple", response_model=SimpleGlossaryResponse, summary="저장된 금융 용어 SIMPLE 설명 조회"
)
def get_simple_glossary(
    term: Annotated[str, Query(min_length=1, max_length=200)],
    repository: Annotated[GlossaryRepository, Depends(get_glossary_repository)],
):
    term = term.strip()

    if not term:
        raise HTTPException(status_code=422, detail="금융 용어를 입력해야 합니다.")

    try:
        return simple_glossary(repository, term)

    except TermNotFoundError:
        raise HTTPException(status_code=404, detail="등록된 금융 용어를 찾을 수 없습니다") from None

    except ExplanationNotFoundError:
        raise HTTPException(status_code=404, detail="저장된 SIMPLE 설명이 없습니다") from None

    except StaleExplanationError:
        raise HTTPException(
            status_code=409, detail="원문이 변경되어 저장된 설명을 검토해야 합니다"
        ) from None

    except SummaryUnavailableError:
        raise HTTPException(status_code=503, detail="용어 설명을 제공할 수 없습니다") from None
