from fastapi import FastAPI

app = FastAPI(title="MONOW AI Service")


@app.get("/health")
def health_check():
    return {"status": "ok"}


