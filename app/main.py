from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.dependencies import get_engine
from app.api.glossary import router


@asynccontextmanager
async def lifespan(app):
    try:
        yield
    finally:
        if get_engine.cache_info().currsize:
            get_engine().dispose()
            get_engine.cache_clear()


app = FastAPI(title="MONOW AI Service", lifespan=lifespan)
app.include_router(router)


@app.get("/health")
def health_check():
    return {"status": "ok"}
