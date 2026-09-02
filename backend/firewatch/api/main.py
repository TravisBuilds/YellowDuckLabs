"""Fire Watch HTTP API."""

from __future__ import annotations

import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from firewatch.api.routers import ai, alerts, geo, municipalities

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

app = FastAPI(
    title="Yellow Duck Labs — Fire Watch",
    version="0.1.0",
    description=(
        "Municipal wildfire operating picture. Every value returned by this API "
        "carries its source dataset, observation date and confidence. Endpoints "
        "return explicit 'unknown' markers rather than omitting missing data."
    ),
)

# The web client runs on a different port in development.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(municipalities.router)
app.include_router(geo.router)
app.include_router(ai.router)
app.include_router(alerts.router)


@app.get("/health", tags=["meta"])
def health() -> dict:
    from sqlalchemy import text

    from firewatch.core.db import engine

    try:
        with engine.connect() as conn:
            postgis = conn.execute(text("SELECT PostGIS_Version()")).scalar()
        return {"status": "ok", "database": "connected", "postgis": postgis}
    except Exception as exc:
        return {"status": "degraded", "database": f"{type(exc).__name__}: {exc}"}
