"""Optional CORS. The API is server-to-server, so it is off unless configured."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import Settings


def install_cors(app: FastAPI, settings: Settings) -> None:
    if not settings.CORS_ALLOW_ORIGINS:
        return
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.CORS_ALLOW_ORIGINS,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "Idempotency-Key"],
        max_age=600,
    )
