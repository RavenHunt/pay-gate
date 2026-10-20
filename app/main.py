"""FastAPI application: client API, provider ingress, internal job and admin routes."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.adapters.faspay.http import close_http_client
from app.config import get_settings
from app.database import dispose_engine
from app.exceptions import register_exception_handlers
from app.middleware.correlation import CorrelationMiddleware
from app.middleware.cors import install_cors
from app.observability import configure_logging, get_logger
from app.routers import admin, health, ingress_legacy, ingress_snap, jobs, oauth, payments
from app.services.delivery import set_delivery_client


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    configure_logging(settings.LOG_LEVEL, settings.DEBUG)
    get_logger("app").info("startup", env=settings.APP_ENV, environment=settings.environment)
    yield
    await close_http_client()
    set_delivery_client(None)
    await dispose_engine()


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="Faspay Payment Gateway Orchestrator",
        version="0.1.0",
        lifespan=lifespan,
        docs_url=None if settings.is_production else "/docs",
        redoc_url=None,
        openapi_url=None if settings.is_production else "/openapi.json",
    )
    register_exception_handlers(app)
    install_cors(app, settings)
    app.add_middleware(CorrelationMiddleware)

    app.include_router(health.router)
    app.include_router(oauth.router)
    app.include_router(payments.router)
    app.include_router(ingress_snap.router)
    app.include_router(ingress_legacy.router)
    app.include_router(jobs.router)
    app.include_router(admin.router)
    return app


app = create_app()
