from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncConnection

from app.repositories.db import Row, execute, fetch_all, fetch_one, load_sql, to_json
from app.utils.ids import uuid7


async def enqueue(
    conn: AsyncConnection,
    job_type: str,
    *,
    ref_id: uuid.UUID | None = None,
    dedupe_key: str | None = None,
    delay_seconds: float = 0,
    max_attempts: int = 8,
    args: dict[str, Any] | None = None,
) -> uuid.UUID | None:
    """Idempotent through `dedupe_key`; returns None when the job already exists."""
    row = await fetch_one(
        conn,
        """
        INSERT INTO gateway_jobs
          (id, job_type, status, dedupe_key, ref_id, args, attempts, max_attempts, run_after)
        VALUES
          (:id, :job_type, 'PENDING', :dedupe_key, :ref_id, CAST(:args AS jsonb), 0, :max_attempts,
           now() + make_interval(secs => CAST(:delay AS double precision)))
        ON CONFLICT (dedupe_key) DO NOTHING
        RETURNING id
        """,
        {
            "id": uuid7(),
            "job_type": job_type,
            "dedupe_key": dedupe_key,
            "ref_id": ref_id,
            "args": to_json(args),
            "max_attempts": max_attempts,
            "delay": float(delay_seconds),
        },
    )
    return row["id"] if row else None


async def claim(
    conn: AsyncConnection,
    *,
    run_id: str,
    batch_size: int,
    lease_seconds: int,
    job_types: list[str] | None = None,
) -> list[Row]:
    params: dict[str, Any] = {
        "run_id": run_id,
        "batch_size": batch_size,
        "lease_seconds": float(lease_seconds),
    }
    if job_types:
        params["job_types"] = job_types
        return await fetch_all(conn, load_sql("claim_jobs_filtered.sql"), params)
    return await fetch_all(conn, load_sql("claim_jobs.sql"), params)


_OWNED = "id = :id AND status = 'RUNNING' AND locked_by = :run_id"


async def mark_done(conn: AsyncConnection, job_id: uuid.UUID, run_id: str) -> bool:
    return (
        await execute(
            conn,
            f"""
            UPDATE gateway_jobs
            SET status = 'DONE', finished_at = now(), locked_until = NULL, locked_by = NULL,
                updated_at = now()
            WHERE {_OWNED}
            """,
            {"id": job_id, "run_id": run_id},
        )
        == 1
    )


async def mark_retry(
    conn: AsyncConnection,
    job_id: uuid.UUID,
    run_id: str,
    *,
    delay_seconds: float,
    error_category: str,
) -> bool:
    return (
        await execute(
            conn,
            f"""
            UPDATE gateway_jobs
            SET status = 'RETRY', locked_until = NULL, locked_by = NULL,
                run_after = now() + make_interval(secs => CAST(:delay AS double precision)),
                last_error_category = :category, updated_at = now()
            WHERE {_OWNED}
            """,
            {
                "id": job_id,
                "run_id": run_id,
                "delay": float(delay_seconds),
                "category": error_category,
            },
        )
        == 1
    )


async def mark_dead(
    conn: AsyncConnection, job_id: uuid.UUID, run_id: str, *, error_category: str
) -> bool:
    return (
        await execute(
            conn,
            f"""
            UPDATE gateway_jobs
            SET status = 'DEAD', finished_at = now(), locked_until = NULL, locked_by = NULL,
                last_error_category = :category, updated_at = now()
            WHERE {_OWNED}
            """,
            {"id": job_id, "run_id": run_id, "category": error_category},
        )
        == 1
    )


async def reset_job(conn: AsyncConnection, dedupe_key: str) -> bool:
    """Operator replay: make a dead job runnable again with a fresh attempt budget."""
    return (
        await execute(
            conn,
            """
            UPDATE gateway_jobs
            SET status = 'PENDING', attempts = 0, run_after = now(), finished_at = NULL,
                locked_until = NULL, locked_by = NULL, updated_at = now()
            WHERE dedupe_key = :dedupe_key AND status IN ('DEAD', 'DONE')
            """,
            {"dedupe_key": dedupe_key},
        )
        == 1
    )


async def due_stats(conn: AsyncConnection) -> Row | None:
    return await fetch_one(
        conn,
        """
        SELECT count(*) FILTER (WHERE status IN ('PENDING', 'RETRY')) AS pending,
               count(*) FILTER (WHERE status = 'DEAD') AS dead,
               COALESCE(EXTRACT(EPOCH FROM (now() - min(run_after)
                   FILTER (WHERE status IN ('PENDING', 'RETRY') AND run_after <= now()))), 0)
                   AS oldest_due_age_seconds
        FROM gateway_jobs WHERE status IN ('PENDING', 'RETRY', 'DEAD')
        """,
    )


async def release(conn: AsyncConnection, job_id: uuid.UUID, run_id: str) -> bool:
    """Give back a claimed job that this run ran out of time for (not counted as an attempt)."""
    return (
        await execute(
            conn,
            f"""
            UPDATE gateway_jobs
            SET status = 'PENDING', attempts = GREATEST(attempts - 1, 0), locked_until = NULL,
                locked_by = NULL, updated_at = now()
            WHERE {_OWNED}
            """,
            {"id": job_id, "run_id": run_id},
        )
        == 1
    )
