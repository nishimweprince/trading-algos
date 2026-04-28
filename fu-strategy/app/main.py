from fastapi import FastAPI

app = FastAPI(
    title="FU Strategy API",
    description="FastAPI scaffold for FU candle / multi-timeframe strategy work.",
    version="0.1.0",
)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/")
def root() -> dict[str, str]:
    return {"service": "fu-strategy"}
