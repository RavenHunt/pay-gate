# Faspay Payment Gateway Orchestrator

One gateway for Fluency Hub, Crowdfunding and later apps to create Faspay payments (**SNAP** and **Legacy**)
and receive routed, signed notifications. Implements the PRD, ERD and TRD in this folder.

- FastAPI (async) · SQLAlchemy 2 + asyncpg (raw SQL at runtime, ORM only for schema/Alembic) · Neon PostgreSQL
- Vercel serverless · Upstash QStash for scheduling · no Celery, no Redis, no worker server
- Database outbox (`gateway_jobs`) claimed with `FOR UPDATE SKIP LOCKED`

## Quick start

```bash
uv venv --python 3.12 && uv pip install -e ".[dev]"
cp .env.example .env                       # fill DATABASE_URL, OAUTH_JWT_SECRET (dev), CLIENT_SECRET_PEPPER
alembic upgrade head                       # needs DATABASE_URL
uvicorn api.index:app --reload             # docs at /docs when APP_ENV != production
```

### Tests

```bash
# PostgreSQL 15 on TEST_DATABASE_URL (default postgresql://postgres@127.0.0.1:54329/gateway_test)
pytest                                     # 162 tests; integration tests skip if Postgres is unreachable
ruff check . && mypy app scripts
```

Integration tests run the real app against real PostgreSQL (migrations applied from scratch) with a fake Faspay
and a fake consumer behind `httpx.MockTransport`; any unmocked outbound request fails the test.

## Administration (audited CLI, no UI)

```bash
python -m scripts.admin --operator alice seed-channels
python -m scripts.admin --operator alice add-channel --method VA --channel-code 402 --protocol SNAP --display-name "BCA VA"
python -m scripts.admin --operator alice create-merchant --code KRN-SBX --environment SANDBOX \
    --credential-ref KRN --provider-merchant-id 12345 --partner-id P1 --merchant-name "Karina"
python -m scripts.admin --operator alice create-client --app-code CROWDFUNDING --name "Crowdfunding" \
    --webhook-url https://cf.example.org/gateway/hook --webhook-secret-ref CROWDFUNDING_WEBHOOK_SECRET
python -m scripts.admin --operator alice add-credential --app-code CROWDFUNDING      # prints the secret once
python -m scripts.admin --operator alice add-route --app-code CROWDFUNDING --merchant-code KRN-SBX \
    --method VA --channel-code 402 --protocol SNAP --default
python -m scripts.admin list-routes --app-code CROWDFUNDING
```

Every change writes `audit_logs` (`actor_type = ADMIN`). Route/status changes wait for the 30 s cache TTL before
reporting done (`--no-wait` skips). Switching a channel between SNAP and Legacy is an `add-route` to the other
protocol's channel plus `set-route-status INACTIVE` on the old one, after the previous outcome is known; the
gateway never fails over on create.

Merchant secrets live in environment variables named from `credential_ref` (`KRN_PRIVATE_KEY`,
`KRN_FASPAY_PUBLIC_KEY`, `KRN_LEGACY_USER`, `KRN_LEGACY_PASSWORD`). Consumer webhook secrets are the variable
named by `webhook_secret_ref`; set `<name>_PREV` while rotating and both secrets sign.

## Deploying

1. **Neon**: use the *pooled* URL. The engine already disables the asyncpg statement cache and uses `NullPool`.
2. **Vercel**: one project per environment (`APP_ENV=sandbox|production`), separate Neon branch and variables.
   `vercel.json` sets `maxDuration` 60 and has **no cron entries**. Add the variables from `.env.example`.
3. **CI**: run `alembic upgrade head` before each deploy (`MIGRATION_DATABASE_URL` may point at the direct,
   non-pooled URL).
4. **QStash schedules** (all POST `https://<host>/internal/jobs/run`):

   | Cadence | Body | Purpose |
   |---|---|---|
   | every minute | `{}` | claim delivery / inquiry / correlation jobs |
   | every 5 minutes | `{"scan": true}` | stale `CREATED`, `UNKNOWN` inquiries, expiry checks |
   | daily | `{"purge": true}` | retention purge |

   Calls are accepted only with a valid `Upstash-Signature` (current or next signing key, URL and body hash
   checked). Set `PUBLIC_BASE_URL` to the exact public origin.
5. **Faspay**: register `https://<host>` as the SNAP merchant URL (paths are served at its root) and
   `https://<host>/v1/webhooks/faspay/legacy/notification` for Legacy.
6. **Platform edge**: add Vercel Firewall rate limits per path (token endpoint tightest, ingress paths,
   `/internal/*`). There is no Redis; the in-app throttles are best-effort only.

## API

| | |
|---|---|
| `POST /v1/oauth/token` | client credentials (Basic or form), RS256/ES256 JWT, 10 min |
| `POST /v1/payments` | needs `Idempotency-Key`; `201` created, `200` replay, `202` outcome `UNKNOWN`, `502 PROVIDER_REJECTED` confirmed failure |
| `GET /v1/payments/{id}` · `/by-reference/{ref}` | scoped to the token's client |
| `POST /v1/payments/{id}/cancel` | `501 CANCEL_NOT_SUPPORTED` (post-MVP) |
| `POST /v1.0/transfer-va/payment`, `/v1.0/qr/qr-mpm-notify`, `/v1.0/debit/notify` | SNAP ingress, one shared handler |
| `POST /v1/webhooks/faspay/legacy/notification` | Legacy ingress |
| `POST /internal/jobs/run` | QStash only |
| `POST /internal/reconciliation/{id}[?inline=true]`, `/internal/deliveries/{id}/replay`, `/internal/webhook-events/{id}/reprocess` | admin token (`Authorization: Bearer`, hash in `ADMIN_API_TOKEN_HASH`, optional `X-Operator`) |
| `GET /health`, `/ready` | liveness, DB readiness |

Errors use `{"error": {"code", "message", "correlation_id"}}`; the token endpoint uses the RFC 6749 shape.
Consumer webhooks carry `X-Gateway-Event-Id`, `X-Gateway-Timestamp`, `X-Gateway-Signature: v1=HMAC_SHA256(secret, ts + "." + body)`;
delivery is at-least-once, so consumers must dedupe on `event_id`.

## Open items are configuration (TRD section 20)

| Open item | Setting | Default |
|---|---|---|
| Signature-only vs signature + inquiry | `CALLBACK_VERIFY_MODE` | `SIGNATURE_AND_INQUIRY` |
| Notification signature header names / path form | `SNAP_CALLBACK_SIGNATURE_HEADERS`, `SNAP_CALLBACK_PATH_MODES` | `X-SIGNATURE_FROM_FASPAY,X-SIGNATURE` / `path` |
| Header does not arrive | `SNAP_ALLOW_UNSIGNED_WITH_INQUIRY` | `true` (inquiry-only, `signature_valid=false`) |
| Duplicate reply (success vs documented 409) | `SNAP_DUPLICATE_ACK_MODE` | `SUCCESS` |
| QRIS query `serviceCode` (51 vs 47), e-wallet status code | `SNAP_QRIS_QUERY_SERVICE_CODE`, `SNAP_EWALLET_STATUS_SERVICE_CODE` | `51`, `55` |
| Status ambiguities (`07`, `01`/`02`, legacy `5`/`9`) | `SNAP_STATUS_MAP_OVERRIDES`, `LEGACY_STATUS_MAP_OVERRIDES` | tables in `status_map.py` |
| Request field details per method | `SNAP_BODY_OVERRIDES`, `LEGACY_BODY_OVERRIDES` (deep-merged) | none |
| Legacy notification signature / reply code | `LEGACY_VERIFY_NOTIFICATION_SIGNATURE`, `LEGACY_ACK_USE_INQUIRY_CODE`, `LEGACY_ERROR_RESPONSE_CODE` | off / echo inquiry / `99` |
| e-wallet notification route | all three routes share one handler; parser chosen by payload shape | |
| Channel codes and enabled methods per merchant | `payment_channels` + `client_merchant_routes` rows (admin CLI) | |
| Default / max `expires_at` | `EXPIRES_DEFAULT_MINUTES_BY_PROTOCOL`, `EXPIRES_MAX_MINUTES_BY_METHOD` | SNAP 5 h, Legacy 1 day |
| SLO, retention, stale thresholds | `RETENTION_*`, `STALE_PENDING_MINUTES_BY_METHOD`, retry settings | ERD 6 / TRD 12.7 |
| Expiry without provider confirmation | `EXPIRY_FORCE_AFTER_MINUTES` | `0` (inquiry-confirmed only) |
| Outbound IP allow-listing | none yet: needs static egress if Faspay requires it | |

## Operations

Structured logs (`structlog`, JSON) carry `correlation_id`, `protocol`, `payment_id`, `client_id`. Metrics are log
events (`event=metric`); alert conditions are `event=alert` with an `alert` code. Alert on at least:
`job_dead`, `delivery_dead_letter`, `callback_amount_mismatch`, `callback_contradicts_final_state`,
`callback_inquiry_disagrees`, `create_lost_marked_unknown`, `unknown_create_not_inquirable`,
`merchant_credentials_unavailable`, a rising `callback_rejected` / `callback_unmatched`, metric
`jobs_oldest_due_age_seconds`, and **absence** of a `job_run` log line for a few minutes (scheduler down).

Runbook: a `FAILED` webhook event (amount mismatch, contradiction) needs a human, then
`POST /internal/reconciliation/{payment_id}?inline=true`. `UNMATCHED` events retry by themselves; after fixing the
cause use `/internal/webhook-events/{id}/reprocess`. Dead deliveries: fix the consumer, then `/replay`.

## Behaviour worth knowing (decisions beyond the TRD text)

- **Claimed paid but not confirmed**: if a notification says paid and the inquiry does not (inquiry-only SNAP, Legacy,
  or `SIGNATURE_AND_INQUIRY`), the gateway does **not** acknowledge (Faspay retries) and queues a follow-up inquiry
  that retries until Faspay agrees. Otherwise a lagging inquiry plus a success ack would strand the payment.
- **Early callbacks**: a callback for a payment still in `CREATED` is held (`UNMATCHED`, not-found reply) and applied
  by `RETRY_CORRELATION` once the create response is persisted. A payment stuck in `CREATED` past
  `CREATED_STALE_MINUTES` becomes `UNKNOWN` (the scan), never silently failed.
- **Create outcomes**: connection never established means confirmed `FAILED`; anything after the request may have
  been written, HTTP 5xx, `409` or a success reply without an identifier is `UNKNOWN`. Never resubmitted and never
  sent through the other protocol. A SNAP VA with a lost response has no inquiry (the VA number is unknown):
  the callback (correlated by `trxId`) recovers it.
- **Synchronous create outcomes are not webhooked**: `payment.failed`/`payment.unknown` caused by the create call are
  recorded but not delivered (the consumer got them in the HTTP response); later resolutions are delivered.
- **Idempotent replay** returns the payment's *current* state, not a stale snapshot.
- **Re-correlation** uses the normalized fields stored with each inbox event, so replay does not depend on headers
  that are not stored.
- Inquiries triggered by callbacks only happen for references the gateway knows, so forged notifications cannot make
  it call Faspay.
- Customer name/email/phone are used for the provider call only and never stored.
- `api_idempotency_keys.resolved_at` is an extra column (retention starts at resolution, never while unresolved).

## Not verified here

Nothing in this repository has run against the real Faspay sandbox, a real Neon/PgBouncer endpoint, Vercel, or live
QStash. Specifically still to confirm before production (they are all settings or isolated adapter code):

- Faspay request field names for VA create and e-wallet (built from the TRD description and the existing
  integration; adjust with `SNAP_BODY_OVERRIDES`), `billDate` format, VA inquiry response placement.
- Notification headers, signature path form, retry policy, `409` behaviour, legacy status table and signature.
- The 500 rps target: `loadtest/locustfile.py` is provided; no result has been recorded.
- PgBouncer transaction-pooling behaviour (connection args are set for it; tests ran on plain PostgreSQL).
