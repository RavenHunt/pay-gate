"""POST /internal/jobs/run: called only by QStash (scheduled runs and nudges)."""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel, ValidationError, field_validator

from app.domain import JobType
from app.exceptions import GatewayError
from app.services import qstash
from app.services.job_runner import run_jobs

router = APIRouter(prefix="/internal/jobs", tags=["internal"], include_in_schema=False)


class JobRunRequest(BaseModel):
    max_jobs: int | None = None
    time_budget_ms: int | None = None
    job_types: list[str] | None = None
    scan: bool = False
    purge: bool = False

    @field_validator("max_jobs", "time_budget_ms")
    @classmethod
    def _positive(cls, value: int | None) -> int | None:
        if value is not None and value <= 0:
            raise ValueError("must be positive")
        return value

    @field_validator("job_types")
    @classmethod
    def _known(cls, value: list[str] | None) -> list[str] | None:
        if value is not None:
            valid = {t.value for t in JobType}
            if not set(value) <= valid:
                raise ValueError("unknown job type")
        return value


@router.post("/run")
async def run(request: Request) -> dict[str, Any]:
    raw = await request.body()
    if not qstash.verify_signature(
        request.headers.get("upstash-signature"), raw, qstash.expected_url(request)
    ):
        raise GatewayError(401, "UNAUTHORIZED", "Invalid QStash signature.")
    try:
        payload = JobRunRequest(**(json.loads(raw) if raw.strip() else {}))
    except (ValueError, TypeError, ValidationError) as exc:
        raise GatewayError(400, "MALFORMED_REQUEST", "Invalid job run request.") from exc
    summary = await run_jobs(
        max_jobs=payload.max_jobs,
        time_budget_ms=payload.time_budget_ms,
        job_types=payload.job_types,
        scan=payload.scan,
        purge=payload.purge,
    )
    return summary.as_dict()
