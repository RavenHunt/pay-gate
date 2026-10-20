"""Bounded, triggered job execution (TRD 12): claim with SKIP LOCKED, run, reschedule."""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from app.config import get_settings
from app.database import transaction
from app.observability import alert, emit_metric, get_logger
from app.repositories import jobs as jobs_repo
from app.services import reconciliation
from app.services.job_errors import DeadJob, RetryJob
from app.services.job_handlers import get_dead_hooks, get_handlers
from app.services.retry import compute_backoff

log = get_logger("jobs")


@dataclass
class RunSummary:
    run_id: str
    claimed: int = 0
    done: int = 0
    retried: int = 0
    dead: int = 0
    released: int = 0
    scan: dict[str, int] = field(default_factory=dict)
    purge_enqueued: bool = False
    elapsed_ms: int = 0

    def as_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


async def run_jobs(
    *,
    max_jobs: int | None = None,
    time_budget_ms: int | None = None,
    job_types: list[str] | None = None,
    scan: bool = False,
    purge: bool = False,
) -> RunSummary:
    settings = get_settings()
    # Caller values are bounded by server-side maximums.
    max_jobs = min(max_jobs or settings.JOBS_DEFAULT_MAX_JOBS, settings.JOBS_MAX_JOBS_CAP)
    budget_ms = min(
        time_budget_ms or settings.JOBS_DEFAULT_TIME_BUDGET_MS, settings.JOBS_TIME_BUDGET_CAP_MS
    )
    started = time.monotonic()
    deadline = started + budget_ms / 1000
    stop_claiming = started + (budget_ms / 1000) * settings.JOBS_STOP_CLAIMING_FRACTION
    run_id = f"run_{uuid.uuid4().hex[:16]}"
    summary = RunSummary(run_id=run_id)
    handlers = get_handlers()

    if scan:
        summary.scan = await reconciliation.scan_and_enqueue()
    if purge:
        summary.purge_enqueued = await reconciliation.enqueue_daily_purge()

    while summary.claimed < max_jobs and time.monotonic() < stop_claiming:
        batch_size = min(settings.JOBS_CLAIM_BATCH_SIZE, max_jobs - summary.claimed)
        async with transaction() as conn:  # the claim is one short transaction
            jobs = await jobs_repo.claim(
                conn,
                run_id=run_id,
                batch_size=batch_size,
                lease_seconds=settings.JOBS_LEASE_SECONDS,
                job_types=job_types,
            )
        if not jobs:
            break
        summary.claimed += len(jobs)
        for job in jobs:
            remaining = deadline - time.monotonic()
            if remaining <= 0.5:
                async with transaction() as conn:
                    await jobs_repo.release(conn, job["id"], run_id)
                summary.released += 1
                continue
            await _execute(job, run_id, handlers, get_dead_hooks(), remaining, summary)

    summary.elapsed_ms = int((time.monotonic() - started) * 1000)
    async with transaction() as conn:
        stats = await jobs_repo.due_stats(conn)
    if stats:
        emit_metric("jobs_pending", float(stats["pending"]))
        emit_metric("jobs_dead", float(stats["dead"]))
        emit_metric("jobs_oldest_due_age_seconds", float(stats["oldest_due_age_seconds"]))
    log.info("job_run", **summary.as_dict())
    return summary


async def _execute(
    job: dict[str, Any],
    run_id: str,
    handlers: dict[str, Any],
    dead_hooks: dict[str, Any],
    remaining: float,
    summary: RunSummary,
) -> None:
    settings = get_settings()
    job_type = job["job_type"]
    handler = handlers.get(job_type)
    if handler is None:
        await _finish_dead(job, run_id, "unknown_job_type", dead_hooks, summary)
        return
    if job["attempts"] > job["max_attempts"]:  # a crash loop through lease expiry
        await _finish_dead(job, run_id, "attempts_exhausted", dead_hooks, summary)
        return

    def backoff() -> float:
        return compute_backoff(
            job["attempts"],
            base=settings.RETRY_BASE_SECONDS,
            cap=settings.RETRY_CAP_SECONDS,
            jitter_fraction=settings.RETRY_JITTER_FRACTION,
        )

    try:
        await asyncio.wait_for(handler(job), timeout=remaining)
    except RetryJob as exc:
        await _retry_or_dead(job, run_id, exc.category, exc.delay or backoff(), dead_hooks, summary)
    except DeadJob as exc:
        await _finish_dead(job, run_id, exc.category, dead_hooks, summary)
    except TimeoutError:
        await _retry_or_dead(job, run_id, "handler_timeout", backoff(), dead_hooks, summary)
    except Exception as exc:  # noqa: BLE001 - one bad job must not stop the run
        log.error(
            "job_handler_error", job_id=str(job["id"]), job_type=job_type,
            error_type=type(exc).__name__, exc_info=exc,
        )  # fmt: skip
        await _retry_or_dead(job, run_id, "handler_error", backoff(), dead_hooks, summary)
    else:
        async with transaction() as conn:
            await jobs_repo.mark_done(conn, job["id"], run_id)
        summary.done += 1


async def _retry_or_dead(
    job: dict[str, Any],
    run_id: str,
    category: str,
    delay: float,
    dead_hooks: dict[str, Any],
    summary: RunSummary,
) -> None:
    if job["attempts"] >= job["max_attempts"]:
        await _finish_dead(job, run_id, category, dead_hooks, summary)
        return
    async with transaction() as conn:
        await jobs_repo.mark_retry(
            conn, job["id"], run_id, delay_seconds=delay, error_category=category
        )
    summary.retried += 1


async def _finish_dead(
    job: dict[str, Any],
    run_id: str,
    category: str,
    dead_hooks: dict[str, Any],
    summary: RunSummary,
) -> None:
    async with transaction() as conn:
        await jobs_repo.mark_dead(conn, job["id"], run_id, error_category=category)
    summary.dead += 1
    alert("job_dead", job_id=str(job["id"]), job_type=job["job_type"], category=category)
    hook = dead_hooks.get(job["job_type"])
    if hook is not None:
        try:
            await hook(job, category)
        except Exception as exc:  # noqa: BLE001
            log.error("dead_hook_error", job_id=str(job["id"]), error_type=type(exc).__name__)
