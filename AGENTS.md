# AGENTS.md

Faspay payment gateway orchestrator (SNAP + Legacy) for internal apps. FastAPI async on Vercel, Neon PostgreSQL.
Specs: `refs/PRD.md` (what/why), `refs/TRD.md` (how), `refs/ERD.md` (schema). `README.md` has ops, API table, config
open items and behaviour decisions. Read the relevant section, not the whole file (headings: `grep -n "^## " refs/TRD.md`).

## Commands

```bash
.venv/bin/python -m pytest -q          # 162 tests; the ~110 integration tests SKIP without PostgreSQL
.venv/bin/python -m ruff check .       # E,F,I,N,W,UP, line length 100
.venv/bin/python -m mypy app scripts   # strict
alembic upgrade head                   # needs DATABASE_URL
uvicorn api.index:app --reload         # local server
```

Integration tests need PostgreSQL 15 at `TEST_DATABASE_URL` (default `postgresql://postgres@127.0.0.1:54329/gateway_test`).
A green run with skips is not a pass for DB-touching changes: start Postgres, or say the tests were skipped.

## Layout (`app/`)

- `routers/` HTTP only: payments, oauth, ingress_snap, ingress_legacy, jobs (QStash), admin, health. Entry: `api/index.py` -> `main.py`.
- `services/` business logic. Payment status rules: `state_machine.py`, `transitions.py` (TRD 11.3). Jobs: `job_runner.py`, `job_handlers.py`.
  Callbacks: `snap_ingress.py`, `legacy_ingress.py`, `event_processor.py`. Reconcile/expiry: `reconciliation.py`. Webhooks to consumers: `delivery.py`.
- `adapters/faspay/{snap,legacy}/` provider signing, verify, client, mappers; `status_map.py`, `registry.py`, `credentials.py`.
- `repositories/` raw SQL via `db.py` helpers (`fetch_one`, `fetch_all`, `execute`); larger queries in `repositories/sql/*.sql`.
- `models/tables.py` SQLAlchemy models, used for schema and Alembic only. `migrations/versions/` Alembic. `schemas/` pydantic API models.
- `config.py` all settings (env). `domain.py` enums (`PaymentStatus`, `JobType`). `scripts/admin.py` audited admin CLI. `tests/` pytest.

## Constraints (verified in code/README)

- Serverless: no long-running workers, no Celery, no Redis. Background work is the `gateway_jobs` outbox claimed with
  `FOR UPDATE SKIP LOCKED` and triggered by QStash POSTs to `/internal/jobs/run`. Do not add Redis or in-memory state that must be shared.
- Neon pooled URL: engine uses `NullPool` and no asyncpg statement cache (`app/database.py`). Keep queries compatible.
- Runtime DB access is raw SQL in `repositories/`; do not introduce ORM queries. Schema change = model edit + new Alembic revision.
- Money is integer IDR (BIGINT, `StrictInt`); no floats. Datetimes are timezone-aware (TIMESTAMPTZ).
- Idempotency is load-bearing: create needs `Idempotency-Key`; inbox/jobs/deliveries rely on unique keys + `ON CONFLICT DO NOTHING`.
  Status changes go through the guarded compare-and-set update, never a bare `UPDATE payments SET status`.
- Never resubmit a create or fail over between SNAP and Legacy; ambiguous outcomes become `UNKNOWN` (README "Behaviour worth knowing").
- Provider uncertainties are settings (`SNAP_*`, `LEGACY_*` overrides in `config.py`), not code branches.

## Security rules

- Secrets only in env vars (names derive from `credential_ref`, see README); never in source, logs, tests, or docs. `.env` is gitignored.
- Use constant-time comparison for signatures/tokens; fail closed when keys or config are missing.
- Log through `structlog`; keys pass `utils/redact.py` / `observability.py`. Do not log headers, bodies, credentials, or customer PII.
- Consumer webhook URLs are SSRF-pinned in `delivery.py`; do not bypass it.
- Do not run destructive DB/deploy actions (`alembic downgrade`, drops, `vercel --prod`) without explicit user approval.

## Working rules

- Find an existing comparable implementation before adding a pattern; match its style. No new dependencies without need.
- Minimal diffs: no drive-by refactors. Keep backward-compatible API/webhook contracts unless told otherwise.
- Do not invent settings, env vars, columns or endpoints; grep `config.py` / `models/tables.py` first.
- Add or update a test in the matching `tests/test_*.py` (fixtures in `tests/conftest.py`: `db`, `api`, `faspay`, `consumer`, `seed`;
  unmocked outbound HTTP fails tests). Before finishing: ruff, mypy, pytest; report exactly what ran, passed, or skipped.
- Update `README.md` or `refs/` only if behaviour, config, or procedure changed; edit the existing section, do not add parallel docs.

## Known risks (from a read-only review on 2026-10-20; not confirmed at runtime)

Check these before touching the area; fix only when asked.
- Unsafe defaults: `SNAP_ALLOW_UNSIGNED_WITH_INQUIRY=true`, `LEGACY_VERIFY_NOTIFICATION_SIGNATURE=false`, `APP_ENV` defaults to `development`, no production-settings validation.
- `snap/verify.py` returns MISSING (not failure) when the Faspay public key is unset or malformed.
- Contradicting callback on a final payment enqueues RECONCILE_PAYMENT, which returns without an inquiry (`reconciliation.py`).
- `repositories/attempts.py` `MAX(attempt_no)+1` race; `repositories/sql/claim_jobs*.sql` has no `max_attempts` check on reclaim.
- Reconcile scan queries use `LIMIT` without `ORDER BY`; UNKNOWN SNAP VA with no `provider_reference` is never inquired or expired.
