from functools import lru_cache

from fastapi import HTTPException
from sqlalchemy.exc import SQLAlchemyError

from db.database import create_database_engine
from db.repositories.glossary import GlossaryRepository


@lru_cache(maxsize=1)
def get_engine():
    return create_database_engine()


def get_glossary_repository():
    try:
        engine = get_engine()
    except (ValueError, SQLAlchemyError):
        raise HTTPException(
            status_code=503, detail="용어 데이터베이스에 연결할 수 없습니다"
        ) from None
    try:
        with engine.connect() as connection:
            yield GlossaryRepository(connection)

    except SQLAlchemyError:
        # DB 오류에 포함될 수 있는 접속 정보와 SQL은 응답에 노출하지 않는다.
        raise HTTPException(
            status_code=503, detail="용어 데이터베이스를 사용할 수 없습니다"
        ) from None
