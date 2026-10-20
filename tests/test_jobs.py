from __future__ import annotations

import asyncio
import json
import uuid

import pytest

from app.config import get_settings
from app.database import transaction
from app.repositories import jobs as jobs_repo
from app.services.job_errors import DeadJob, RetryJob
from app.services.job_runner import run_jobs
from tests.helpers import q, q1, qstash_headers, run

pytestmark = pytest.mark.integration

RUN_URL = "https://gateway.test/internal/jobs/run"


async def enqueue(job_type="INQUIRE_PAYMENT", *, max_attempts=3, delay=0, key=None) -> uuid.UUID:
    async with transaction() as conn:
        job_id = await jobs_repo.enqueue(
            conn,
            job_type,
            ref_id=uuid.uuid4(),
            dedupe_key=key or uuid.uuid4().hex,
            delay_seconds=delay,
            max_attempts=max_attempts,
        )
    assert job_id
    return job_id


def use_handlers(monkeypatch, handler, job_type="INQUIRE_PAYMENT"):
    monkeypatch.setattr("app.services.job_runner.get_handlers", lambda: {job_type: handler})


async def test_dedupe_key_makes_enqueue_idempotent(db):
    await enqueue(key="same")
    async with transaction() as conn:
        assert await jobs_repo.enqueue(conn, "INQUIRE_PAYMENT", dedupe_key="same") is None
    assert (await q1("SELECT count(*) AS n FROM gateway_jobs"))["n"] == 1


async def test_parallel_runs_never_process_the_same_job(db, monkeypatch):
    seen: list[uuid.UUID] = []

    async def handler(job):
        seen.append(job["id"])
        await asyncio.sleep(0.01)

    use_handlers(monkeypatch, handler)
    get_settings().JOBS_CLAIM_BATCH_SIZE = 3
    ids = [await enqueue() for _ in range(30)]
    summaries = await asyncio.gather(*[run_jobs(max_jobs=30) for _ in range(4)])
    assert sorted(seen) == sorted(ids)  # every job exactly once
    assert sum(s.done for s in summaries) == 30
    assert (await q1("SELECT count(*) AS n FROM gateway_jobs WHERE status='DONE'"))["n"] == 30
    assert (await q1("SELECT max(attempts) AS m FROM gateway_jobs"))["m"] == 1


async def test_expired_lease_is_reclaimed(db, monkeypatch):
    ran = []

    async def handler(job):
        ran.append(job["id"])

    use_handlers(monkeypatch, handler)
    job_id = await enqueue()
    # A crashed run left the job RUNNING with a lease that has not expired: not claimable.
    await run(
        "UPDATE gateway_jobs SET status='RUNNING', locked_by='dead', attempts=1, "
        "locked_until = now() + interval '5 minutes'"
    )
    assert (await run_jobs()).claimed == 0
    await run("UPDATE gateway_jobs SET locked_until = now() - interval '1 second'")
    summary = await run_jobs()
    assert summary.done == 1 and ran == [job_id]
    assert (await q1("SELECT attempts, status FROM gateway_jobs")) == {
        "attempts": 2,
        "status": "DONE",
    }


async def test_future_jobs_wait_and_types_can_be_filtered(db, monkeypatch):
    ran = []

    async def handler(job):
        ran.append(job["job_type"])

    monkeypatch.setattr(
        "app.services.job_runner.get_handlers",
        lambda: {"INQUIRE_PAYMENT": handler, "EXPIRE_PAYMENT": handler},
    )
    await enqueue("INQUIRE_PAYMENT", delay=3600)
    await enqueue("EXPIRE_PAYMENT")
    await enqueue("INQUIRE_PAYMENT")
    summary = await run_jobs(job_types=["EXPIRE_PAYMENT"])
    assert summary.done == 1 and ran == ["EXPIRE_PAYMENT"]
    assert (await run_jobs()).done == 1  # the due INQUIRE job; the future one still waits


async def test_retry_backoff_then_dead(db, monkeypatch):
    async def handler(job):
        raise RetryJob("flaky")

    use_handlers(monkeypatch, handler)
    get_settings().RETRY_BASE_SECONDS = 100
    await enqueue(max_attempts=3)
    first = await run_jobs()
    assert first.retried == 1
    job = await q1(
        "SELECT status, attempts, last_error_category, run_after > now() AS later FROM gateway_jobs"
    )
    assert job == {"status": "RETRY", "attempts": 1, "last_error_category": "flaky", "later": True}
    assert (await run_jobs()).claimed == 0  # backoff not elapsed

    for _ in range(2):
        await run("UPDATE gateway_jobs SET run_after = now()")
        summary = await run_jobs()
    assert summary.dead == 1
    dead = await q1("SELECT status, attempts, finished_at IS NOT NULL AS fin FROM gateway_jobs")
    assert dead == {"status": "DEAD", "attempts": 3, "fin": True}


async def test_permanent_failure_and_unexpected_errors(db, monkeypatch):
    async def handler(job):
        if job["dedupe_key"] == "perm":
            raise DeadJob("nope")
        raise RuntimeError("bug")

    use_handlers(monkeypatch, handler)
    await enqueue(key="perm")
    await enqueue(key="bug")
    summary = await run_jobs()
    assert summary.dead == 1 and summary.retried == 1  # one bad job does not stop the run
    bug = await q1("SELECT status, last_error_category FROM gateway_jobs WHERE dedupe_key='bug'")
    assert bug == {"status": "RETRY", "last_error_category": "handler_error"}


async def test_unknown_job_type_goes_dead(db, monkeypatch):
    monkeypatch.setattr("app.services.job_runner.get_handlers", lambda: {})
    await enqueue()
    assert (await run_jobs()).dead == 1


async def test_a_slow_job_cannot_overrun_the_budget(db, monkeypatch):
    async def slow(job):
        await asyncio.sleep(5)

    use_handlers(monkeypatch, slow)
    await enqueue()
    summary = await run_jobs(time_budget_ms=1500)
    assert summary.retried == 1  # timed out inside the budget, rescheduled
    assert summary.elapsed_ms < 3000


async def test_unstarted_claimed_jobs_are_released_not_lost(db, monkeypatch):
    async def handler(job):
        await asyncio.sleep(0.3)

    use_handlers(monkeypatch, handler)
    get_settings().JOBS_CLAIM_BATCH_SIZE = 10
    get_settings().JOBS_STOP_CLAIMING_FRACTION = 1.0
    for _ in range(6):
        await enqueue()
    summary = await run_jobs(time_budget_ms=1000)
    assert summary.done >= 1 and summary.released >= 1
    rows = await q("SELECT status, attempts FROM gateway_jobs")
    assert {r["status"] for r in rows} <= {"DONE", "PENDING"}
    assert all(r["attempts"] == 0 for r in rows if r["status"] == "PENDING")  # attempt refunded
    await run_jobs()  # a later run picks the released ones up
    assert (await q1("SELECT count(*) AS n FROM gateway_jobs WHERE status='DONE'"))[
        "n"
    ] >= summary.done


async def test_finish_requires_the_lease(db):
    job_id = await enqueue()
    async with transaction() as conn:
        (claimed,) = await jobs_repo.claim(conn, run_id="run_a", batch_size=1, lease_seconds=60)
        assert claimed["id"] == job_id
        # A run that lost its lease cannot overwrite a newer one.
        assert await jobs_repo.mark_done(conn, job_id, "run_b") is False
        assert await jobs_repo.mark_done(conn, job_id, "run_a") is True


# ── POST /internal/jobs/run (QStash only) ────────────────────────────────


async def test_run_endpoint_rejects_everything_but_qstash(api, monkeypatch):
    body = json.dumps({"max_jobs": 5}).encode()
    assert (await api.post("/internal/jobs/run", content=body)).status_code == 401
    forged = {"Upstash-Signature": "a.b.c"}
    assert (await api.post("/internal/jobs/run", content=body, headers=forged)).status_code == 401
    wrong_key = qstash_headers(RUN_URL, body, key="not-a-qstash-key")
    assert (
        await api.post("/internal/jobs/run", content=body, headers=wrong_key)
    ).status_code == 401
    wrong_url = qstash_headers("https://evil.test/internal/jobs/run", body)
    assert (
        await api.post("/internal/jobs/run", content=body, headers=wrong_url)
    ).status_code == 401
    other_body = qstash_headers(RUN_URL, b'{"max_jobs": 99}')
    assert (
        await api.post("/internal/jobs/run", content=body, headers=other_body)
    ).status_code == 401


async def test_run_endpoint_accepts_current_and_next_keys(api, monkeypatch):
    ran = []

    async def handler(job):
        ran.append(1)

    use_handlers(monkeypatch, handler)
    await enqueue()
    await enqueue()
    for key in (
        "qstash-current-key-for-tests-0123456789",
        "qstash-next-key-for-tests-0123456789ab",
    ):
        body = json.dumps({"max_jobs": 1}).encode()
        resp = await api.post(
            "/internal/jobs/run", content=body, headers=qstash_headers(RUN_URL, body, key)
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["done"] == 1
    assert len(ran) == 2


async def test_run_endpoint_validates_and_caps_the_request(api, monkeypatch):
    for bad in ({"max_jobs": 0}, {"job_types": ["NOPE"]}, {"time_budget_ms": -1}):
        body = json.dumps(bad).encode()
        resp = await api.post(
            "/internal/jobs/run", content=body, headers=qstash_headers(RUN_URL, body)
        )
        assert resp.status_code == 400, bad

    seen = {}

    async def fake_run(**kwargs):
        seen.update(kwargs)
        from app.services.job_runner import RunSummary

        return RunSummary(run_id="r")

    monkeypatch.setattr("app.routers.jobs.run_jobs", fake_run)
    body = b""
    resp = await api.post("/internal/jobs/run", content=body, headers=qstash_headers(RUN_URL, body))
    assert resp.status_code == 200 and seen["scan"] is False


async def test_run_caps_are_enforced_server_side(db, monkeypatch):
    claimed = []

    async def handler(job):
        claimed.append(job["id"])

    use_handlers(monkeypatch, handler)
    settings = get_settings()
    settings.JOBS_MAX_JOBS_CAP = 2
    for _ in range(5):
        await enqueue()
    summary = await run_jobs(max_jobs=1000)
    assert summary.claimed == 2 and len(claimed) == 2
