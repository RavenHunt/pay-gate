# Technical Requirements / Technical Design Document (TRD)

## Faspay Payment Gateway Orchestrator (SNAP + Legacy)

-   **Document status:** Draft for engineering review (brainstorming)
-   **Version:** 1.0
-   **Date:** 12 October 2026 (last revised 20 October 2026)
-   **Proposed stack:** Python 3.12, FastAPI (async), SQLAlchemy 2.0 +
    asyncpg, Neon PostgreSQL (single database), deployed on Vercel
    serverless functions. Database outbox with `FOR UPDATE SKIP LOCKED`
    job claiming, triggered by Upstash QStash. No Celery, no Redis
    queue, no dedicated worker server.
-   **Related documents:** PRD and ERD for Faspay Payment Gateway
    Orchestrator
-   **Reference implementation:** `ki-cf-v5` (project layout, config,
    async DB, error handling style) and the legacy Lumen
    `CntFaspaysnapController` / `CntFaspayController` (field mapping).

## 1. Purpose

This document defines the proposed implementation design for a central
gateway used by multiple applications (MVP: Fluency Hub and
Crowdfunding) to create payments through Faspay and receive routed
payment notifications. It supports two Faspay protocols, **SNAP** and
**Legacy**, shared and dedicated merchant accounts, idempotent
operations, callback delivery, retries, reconciliation, security,
observability, and incremental migration.

Items that the Faspay documentation does not settle are listed in
section 20 (release gates) and must be confirmed against sandbox
behavior before production.

Sources used for the provider contracts: Faspay SNAP Signature, SNAP
Virtual Account, SNAP QRIS, SNAP Direct Debit, Retail Payment
Integration Guide, legacy Payment Notification pages at `docs.faspay.co.id`, plus the existing
PHP and Python integrations in the repositories above.

## 2. Architecture

### 2.1 Logical components

-   **Client Registry / OAuth:** Client identities, credentials, token
    issuance, status, and approved callback destination.
-   **Merchant Registry and Channel Catalogue:** Merchant/account
    metadata, environment, credential prefix, and the methods/channels
    with the protocol that serves each.
-   **Routing Engine:** Resolves the allowed route for an authenticated
    client, payment method, and channel.
-   **Payment Application Service:** Validates requests, creates
    transaction records, coordinates provider calls, and exposes
    normalized responses.
-   **Faspay Adapter (two drivers, one interface):**
    -   **SNAP driver:** RSA request signing, SNAP headers, VA / QRIS /
        e-wallet request mapping, SNAP callback verification.
    -   **Legacy driver:** `SHA1(MD5(...))` signing, Post Data
        Transaction mapping, inquiry-based callback verification,
        retail channels.
-   **Webhook Ingress (two separate routers):** SNAP ingress and Legacy
    ingress, each validating its own protocol, persisting the inbox
    event, and acknowledging in the format its protocol requires.
-   **Event Processor:** Correlates provider event to transaction and
    applies guarded status transitions inside one database transaction.
-   **Job Runner:** Claims due rows from `gateway_jobs` with `SKIP
    LOCKED`, executes them in a bounded batch, and reschedules with
    exponential backoff.
-   **Delivery Dispatcher (job handler):** Delivers signed normalized
    events to approved consumer endpoints.
-   **Reconciliation (job handlers):** Checks stale, unknown, or
    unmatched transactions through protocol-appropriate inquiry.
-   **Audit/Observability:** Structured logs, metrics, and
    administrative audit records.

### 2.2 Deployment model

One codebase and one Vercel project per environment (sandbox and
production use separate projects or environments, separate Neon
branches/databases, and separate environment variables). The same app
exposes independently scalable serverless entry points:

1.  Client API (`/v1/...`).
2.  SNAP ingress (`/v1.0/...`) and Legacy ingress
    (`/v1/webhooks/faspay/legacy/...`).
3.  Job-run endpoint (`/internal/jobs/run`), called only by QStash
    (scheduled runs and post-commit nudges).

There are no continuous workers. Periodic execution comes from an
**external scheduler (Upstash QStash)**. Frequent Vercel Hobby Cron
Jobs must not be relied on.

### 2.3 Sequence diagrams

#### SNAP payment (VA / QRIS / e-wallet)

``` mermaid
sequenceDiagram
    participant App as Consumer App
    participant API as Gateway API (Vercel)
    participant DB as Neon PostgreSQL
    participant F as Faspay SNAP
    participant IN as SNAP Ingress
    participant Q as QStash
    participant J as Job Runner

    App->>API: POST /v1/oauth/token (client credentials)
    API-->>App: access_token (JWT)
    App->>API: POST /v1/payments + Idempotency-Key
    API->>DB: Claim idempotency key, insert payment + attempt (commit)
    API->>F: SNAP create (RSA signed, X-EXTERNAL-ID)
    F-->>API: referenceNo / VA / QR
    API->>DB: Persist provider refs + status + instruction
    API-->>App: Normalized payment response

    F->>IN: POST /v1.0/transfer-va/payment, /v1.0/qr/qr-mpm-notify, or /v1.0/debit/notify
    IN->>IN: Verify SNAP signature (Faspay public key)
    IN->>F: Inquiry confirm (default SIGNATURE_AND_INQUIRY mode)
    IN->>DB: One transaction: inbox event, guarded transition, payment event, delivery + job
    IN-->>F: SNAP success response code
    IN-)Q: Nudge (best effort)
    Q->>J: POST /internal/jobs/run (signed)
    J->>DB: Claim jobs (SKIP LOCKED)
    J->>App: Signed normalized webhook
    App-->>J: 2xx
    J->>DB: Mark delivered
```

#### Legacy payment (retail Alfagroup / Indomaret)

``` mermaid
sequenceDiagram
    participant App as Consumer App
    participant API as Gateway API
    participant DB as Neon PostgreSQL
    participant F as Faspay Legacy
    participant IN as Legacy Ingress
    participant J as Job Runner

    App->>API: POST /v1/payments (method RETAIL)
    API->>DB: Insert payment + attempt (commit)
    API->>F: POST /cvr/300011/10 (Post Data Transaction, SHA1(MD5) signature)
    F-->>API: trx_id (payment code), redirect_url
    API->>DB: Persist trx_id, instruction
    API-->>App: Normalized payment response (payment_code)

    F->>IN: POST /v1/webhooks/faspay/legacy/notification
    IN->>F: Inquiry POST /cvr/100004/10 (bill_no, trx_id)
    F-->>IN: payment_status_code
    IN->>DB: One transaction: inbox event, guarded transition, payment event, delivery + job
    IN-->>F: Legacy notification response
    J->>App: Signed normalized webhook
```

## 3. Project Structure (FastAPI, modelled on `ki-cf-v5`)

```
payment-service/
├── pyproject.toml            # uv; ruff, mypy strict, pytest-asyncio
├── alembic.ini
├── migrations/               # Alembic (ORM metadata -> DDL)
├── api/index.py              # Vercel entry point exposing `app`
├── vercel.json               # function config; NO frequent cron
├── .env.example
├── app/
│   ├── main.py               # FastAPI app, lifespan, router registration
│   ├── config.py             # pydantic-settings; env secret resolution
│   ├── database.py           # single async engine (Neon pooled URL)
│   ├── dependencies.py       # auth (JWT), DB session, correlation ID
│   ├── exceptions.py         # error envelope + handlers
│   ├── middleware/
│   │   ├── correlation.py    # correlation ID, structured log context
│   │   └── cors.py
│   ├── routers/
│   │   ├── health.py
│   │   ├── oauth.py          # POST /v1/oauth/token
│   │   ├── payments.py       # /v1/payments...
│   │   ├── ingress_snap.py   # /v1.0/transfer-va/payment, /v1.0/qr/qr-mpm-notify, /v1.0/debit/notify
│   │   ├── ingress_legacy.py # /v1/webhooks/faspay/legacy/notification
│   │   ├── jobs.py           # /internal/jobs/* (QStash-signed)
│   │   └── admin.py          # /internal/reconciliation, replay
│   ├── schemas/              # Pydantic: payments, events, errors, oauth
│   ├── services/
│   │   ├── payment_service.py
│   │   ├── routing.py
│   │   ├── idempotency.py
│   │   ├── state_machine.py
│   │   ├── event_processor.py
│   │   ├── job_runner.py     # claim / execute / backoff
│   │   ├── delivery.py       # consumer webhook signing + SSRF guard
│   │   └── reconciliation.py
│   ├── adapters/faspay/
│   │   ├── base.py           # FaspayAdapter protocol (interface)
│   │   ├── snap/             # client.py, signing.py, mappers.py, verify.py
│   │   ├── legacy/           # client.py, signing.py, mappers.py, verify.py
│   │   └── status_map.py     # per-protocol status tables
│   ├── repositories/         # raw SQL (text()) per aggregate
│   │   └── sql/              # .sql files for the larger queries
│   └── models/               # SQLAlchemy ORM: schema + Alembic only
└── tests/
```

Differences from `ki-cf-v5`:

-   **No tenant middleware.** There is one database. The client identity
    comes from the OAuth token, not from `x-original-forwarded-for`.
-   **No HMAC `/openapi` middleware.** OAuth and QStash/Faspay signature
    verification replace it.
-   **Lean dependencies.** Do not carry over `celery`, `weasyprint`,
    `aiomysql`, `pymysql`, or Redis; they inflate the serverless bundle
    and cold start.
-   **Dependencies:** `fastapi`, `uvicorn` (local), `sqlalchemy[asyncio]`,
    `asyncpg`, `alembic`, `pydantic`, `pydantic-settings`, `httpx`,
    `cryptography` (RSA), `pyjwt`, `structlog`.
-   **Raw SQL for runtime.** Repositories execute `text()` statements
    with bound parameters (as `ki-cf-v5` does). ORM models are used only
    to define the schema and generate migrations.
-   **Error envelope** follows section 7 rather than the Lumen-compatible
    `{"message": ...}` format, because there is no legacy contract to
    preserve here.

## 4. Runtime Constraints: Vercel and Neon

-   **Function duration.** Every request and job run must finish inside
    the configured `maxDuration` for the plan (proposed: 60 s; confirm
    against the Vercel plan limits). Job runs use a time
    budget: stop claiming new jobs once about 70% of the budget is used,
    and leave leases short so unfinished rows are reclaimed.
-   **Cold starts.** Import adapters lazily where possible, keep the
    dependency list small, and build `httpx.AsyncClient` once per
    instance (module-level) rather than per request.
-   **No in-process state as truth.** Instances are ephemeral and
    parallel. In-memory caches (merchant, channel, client, and credential rows
    with a 30 s TTL, JWT public key material) are an optimization only.
    Route rows use the same TTL, so an administrator route change
    (including a protocol switch) is fully effective 30 s after it is
    made; the admin script waits that long before reporting completion.
-   **Neon connections.** Use the **pooled** connection string. Under
    PgBouncer transaction pooling, disable the asyncpg statement cache
    (`statement_cache_size=0`) and use `NullPool` in the application
    pool, exactly as `ki-cf-v5/app/database.py` does. Do not use
    session-level advisory locks, `LISTEN/NOTIFY`, or temp tables.
    Keep transactions short; never hold a transaction across a provider
    call.
-   **Background work after the response is not guaranteed.** Do not use
    `BackgroundTasks` for anything that must happen. Everything that
    must happen is a `gateway_jobs` row.
-   **Outbound IP addresses are not static** on standard Vercel plans.
    If Faspay requires IP allow-listing of the merchant server, static
    egress must be arranged (see open items).
-   **Inbound IP allow-listing of Faspay** is not a dependable control on
    this platform. Rely on signature/inquiry verification and optional
    Vercel Firewall rules.
-   **Rate limiting** has no Redis in the MVP. Use Vercel Firewall /
    WAF rate-limit rules at the platform edge per path, plus coarse
    per-client limits in the database if needed. Upstash Redis can be
    added later for precise limits.

### 4.1 Throughput target: 500 rps, all traffic combined

The target covers token requests, create, status reads, callback
ingress, and internal job calls together. Free-tier compute and
connection limits on Vercel Hobby and Neon free are likely to cap
sustained throughput below this; the target must be validated, not
assumed.

Design rules that protect the hot path:

-   **Token validation is stateless** (JWT signature + expiry + `kid`),
    with no database call per request. Client status and revocation are
    checked against a short-TTL in-memory cache backed by one indexed
    query.
-   **Status read:** one indexed query by `(client_id, id)` or
    `(client_id, merchant_reference)`.
-   **Create:** one short transaction before the provider call (claim
    idempotency, insert payment and attempt) and one after (persist
    outcome and identifiers), plus one provider call.
    Provider latency dominates; Vercel's concurrency model must be
    verified for I/O-bound waits.
-   **Callback ingress:** one transaction with a single write path
    (inbox insert, payment update, payment event, delivery + job insert).
-   **No synchronous consumer delivery** and no synchronous audit
    fan-out on any hot path.
-   Minimal indexes (see ERD) and small rows; no large JSON indexing.

Acceptance: a documented load test (for example Locust, as in
`ki-cf-v5`'s `bench` extra) against a production-like Neon branch,
reporting sustained rps, p95/p99 latency per endpoint class, error rate,
and the observed bottleneck. If the free tier cannot meet 500 rps, the
result and the scaling path (paid Neon compute, Vercel Pro limits,
read caching, Redis, splitting ingress) are recorded in this document.
Proposed starting latency objectives, to be agreed: status read p95
under 300 ms and callback ingress p95 under 500 ms, excluding provider
call time.

## 5. Authentication and Authorization (OAuth 2.0 Client Credentials)

### 5.1 Token endpoint

`POST /v1/oauth/token`

-   `Content-Type: application/x-www-form-urlencoded`.
-   Body: `grant_type=client_credentials`, optional `scope`.
-   Client authentication: HTTP Basic (`oauth_client_id:secret`) or
    `client_id` / `client_secret` form fields.
-   Verify the secret against `client_credentials.secret_hash` in
    constant time; reject revoked/expired credentials and inactive
    clients.
-   Response:

``` json
{
  "access_token": "<jwt>",
  "token_type": "Bearer",
  "expires_in": 600,
  "scope": "payments:write payments:read"
}
```

-   The JWT is signed by the gateway with RS256 or ES256 (`kid`-based
    rotation needs an asymmetric key pair; HS256 with `OAUTH_JWT_SECRET`
    is for local development only). Claims: `iss`, `sub` (= `client_id`),
    `cred` (= the credential `id`), `aud`, `iat`, `exp`, `jti`, `scope`,
    `kid`. Short lifetime (suggested 10 minutes).
-   Failures return the OAuth error shape (`invalid_client`,
    `invalid_request`, `invalid_scope`) with the standard status codes.
-   Throttle the token endpoint more tightly than other paths.

### 5.2 Protected endpoints

-   `Authorization: Bearer <access_token>`.
-   `client_id` is taken only from the token. A body-supplied client or
    app code is ignored.
-   Scopes: `payments:write` (create; cancel when it is added),
    `payments:read` (status).
-   Revocation: client status (`sub`) and credential status (`cred`)
    are both checked per request against the short-TTL status cache, so
    client deactivation or credential revocation takes effect within the
    cache TTL (30 seconds), not the token lifetime. Key rotation uses
    `kid` with overlapping keys.
-   Provider ingress (`/v1.0/...`, legacy notification) and
    `/internal/jobs/*` do **not** use client OAuth; they use their own
    verification (sections 9.2, 9.3, 12.2).
-   Admin endpoints (`/internal/reconciliation/*`,
    `/internal/deliveries/*/replay`, `/internal/webhook-events/*/reprocess`)
    require `Authorization: Bearer <admin token>`, compared in constant
    time against the SHA-256 hash in `ADMIN_API_TOKEN_HASH`. They are
    rate-limited at the platform edge, and every call writes an
    `audit_logs` row with `actor_type = ADMIN`.

## 6. API Contract

Base URL: `https://payment-gateway.example.com`

### 6.1 Unified payload principle

Fluency Hub and Crowdfunding (and any later platform) use the **same
request, response, and event shapes**. Platform-specific data travels in
`metadata`. Provider and protocol specifics never leak into the contract
except through `payment_instruction`, whose fields are populated per
method.

Identifier mapping for existing schemas:

  -----------------------------------------------------------------------
  Existing identifier           Carried as
  ----------------------------- -----------------------------------------
  `id_transaksi` / `id_donasi`  `merchant_reference` (unique per client)

  Faspay `trx_id` (legacy)      `provider_reference`; for `RETAIL` also
                                `payment_instruction.payment_code`

  SNAP `referenceNo`            `provider_reference`

  Virtual account number        `payment_instruction.virtual_account_no`

  Platform keys (program,       `metadata` (string map, size-limited)
  company, tenant, user)        
  -----------------------------------------------------------------------

### 6.2 Create payment

`POST /v1/payments`

Headers:

-   `Authorization: Bearer <access_token>`
-   `Idempotency-Key: <client-generated-key>` (required, at most 128
    characters; `400` if missing)
-   `Content-Type: application/json`

Request example:

``` json
{
  "merchant_reference": "INV-1001",
  "amount": 150000,
  "currency": "IDR",
  "payment_method": "VA",
  "channel_code": "402",
  "route_code": "DEFAULT",
  "description": "Donasi",
  "customer": {
    "name": "Contoh Pelanggan",
    "email": "customer@example.com",
    "phone": "081234567890"
  },
  "expires_at": "2026-10-21T23:59:59+07:00",
  "metadata": {
    "platform": "CROWDFUNDING",
    "program_id": "123"
  }
}
```

Validation:

-   Authenticated client is active and holds `payments:write`.
-   `merchant_reference` is present and within length/character limits.
-   `amount` is a positive integer number of rupiah within configured
    limits (no floating point); the adapter converts to the provider
    format.
-   Currency (`IDR`) and payment method are allowlisted.
-   `payment_method` and `channel_code` resolve to an active route for
    this client and environment, and to exactly one protocol.
-   `channel_code` is the Faspay channel code from the catalogue (`402`
    in the examples is illustrative); the bank or outlet name is the
    catalogue `display_name`.
-   `route_code` is optional. When omitted, the client's default route
    (`is_default`) for the payment method is used.
-   Customer fields are optional where the provider allows; the
    adapter fills documented defaults (for example the default customer
    name used by the existing integration).
-   `expires_at` is optional; each channel has a default and a
    maximum. (The existing integrations default to 5 hours for SNAP and
    1 day for legacy; confirm the intended product behavior.)
-   `metadata` has bounded key count and value length.
-   Destination callback URL is not accepted as a request field.

Response for a confirmed creation:

``` json
{
  "payment_id": "0199f3d1-aaaa-7bbb-8ccc-123456789abc",
  "merchant_reference": "INV-1001",
  "status": "PENDING",
  "amount": 150000,
  "currency": "IDR",
  "payment_method": "VA",
  "channel_code": "402",
  "provider_reference": "8888801234567890",
  "payment_instruction": {
    "type": "VA",
    "virtual_account_no": "8888801234567890",
    "payment_code": null,
    "qr_url": null,
    "redirect_url": "https://...",
    "expires_at": "2026-10-21T23:59:59+07:00"
  },
  "metadata": { "platform": "CROWDFUNDING", "program_id": "123" },
  "created_at": "2026-10-20T09:30:00+07:00"
}
```

`payment_instruction` fields by method: `VA` fills
`virtual_account_no` and optionally `redirect_url`; `QRIS` fills
`qr_url` (QR image URL); `EWALLET` fills `redirect_url`; `RETAIL`
fills `payment_code` (the legacy `trx_id`) and `redirect_url`. Unused
fields are `null`. For `VA`, `provider_reference` is the
Faspay-assigned virtual account number. This is the internal gateway
contract, not a raw Faspay response. The protocol is not part of the
response.

### 6.3 Get payment

`GET /v1/payments/{payment_id}`

-   Scoped to the authenticated client.
-   Returns normalized status, amount, currency, method, channel,
    merchant reference, provider reference, instruction, metadata, and
    timestamps (`created_at`, `paid_at`, `expires_at`).
-   Never returns credentials, raw authorization headers, signatures, or
    internal secrets.

### 6.4 Get payment by client reference

`GET /v1/payments/by-reference/{merchant_reference}`

-   Must query by both authenticated `client_id` and
    `merchant_reference`.
-   Return not found without revealing whether another client owns the
    reference.
-   Several payments can share a reference over time (a new payment
    after `FAILED`, `EXPIRED`, or `CANCELLED`). Return the most recently
    created one; earlier ones stay reachable by `payment_id`.

### 6.5 Cancel payment

`POST /v1/payments/{payment_id}/cancel`

-   **Post-MVP.** The endpoint is not exposed in the MVP; if it is
    routed before a channel supports it, it returns
    `501 CANCEL_NOT_SUPPORTED`. When added, implement it only for
    channels whose protocol supports cancellation in the current state.
-   Apply ownership checks, idempotency, and provider-specific state
    validation.
-   A cancel request being accepted is not a confirmed cancellation.

### 6.6 Consumer webhook delivery

Headers:

-   `X-Gateway-Event-Id: <event-id>`
-   `X-Gateway-Timestamp: <unix-seconds>`
-   `X-Gateway-Signature: v1=<signature>`

Signing input:

`HMAC_SHA256(client_webhook_secret, timestamp + "." + raw_request_body)`

Use exact raw body bytes, constant-time comparison, timestamp tolerance,
replay protection, and secret rotation. The secret is read from the
environment variable named by `payment_clients.webhook_secret_ref`.

Event payload (identical for every consumer platform):

``` json
{
  "event_id": "evt_0199f3d1",
  "event_type": "payment.succeeded",
  "payment": {
    "payment_id": "0199f3d1-aaaa-7bbb-8ccc-123456789abc",
    "merchant_reference": "INV-1001",
    "provider_reference": "8888801234567890",
    "status": "SUCCEEDED",
    "amount": 150000,
    "currency": "IDR",
    "payment_method": "VA",
    "channel_code": "402",
    "paid_at": "2026-10-20T09:34:15+07:00",
    "metadata": { "platform": "CROWDFUNDING", "program_id": "123" }
  },
  "occurred_at": "2026-10-20T09:34:15+07:00"
}
```

Event types: `payment.created`, `payment.pending`, `payment.succeeded`,
`payment.failed`, `payment.expired`, `payment.cancelled`,
`payment.unknown`. `event_id` is the `payment_events.id` (prefixed
`evt_`) and stays the same across retries. `payment.created` and
`payment.pending` are recorded but not delivered by default (the
consumer has that state from the create response); a per-client
subscription list is post-MVP. Consumers must persist processed event
IDs and handle duplicate deliveries idempotently.

## 7. Error Contract

Use a consistent response envelope:

``` json
{
  "error": {
    "code": "IDEMPOTENCY_KEY_REUSED",
    "message": "The idempotency key was previously used with a different request.",
    "correlation_id": "corr_0199f3d1"
  }
}
```

Suggested HTTP behavior:

-   `400 Bad Request`: malformed request.
-   `401 Unauthorized`: missing or invalid token.
-   `403 Forbidden`: client inactive, scope missing, or route not
    authorized.
-   `404 Not Found`: transaction not found within the client's scope.
-   `409 Conflict`: idempotency key reused with different payload, or a
    uniqueness conflict.
-   `422 Unprocessable Entity`: valid syntax but invalid business
    fields.
-   `429 Too Many Requests`: rate limit exceeded.
-   `5xx`: gateway/provider dependency failure, with a clear distinction
    between confirmed failure and unknown outcome.

For ambiguous provider outcomes, return the stable status `UNKNOWN`
with a correlation ID. Do not invite clients to blindly create another
payment. Map FastAPI validation errors to this envelope (not to `500`,
which `ki-cf-v5` does only to mimic Lumen).

## 8. Merchant and Channel Routing

Routing sequence:

1.  Authenticate the client (token).
2.  Validate payment method, currency, and environment.
3.  Resolve the active route for `(client_id, route_code, method,
    channel_code)` through `client_merchant_routes` joined to
    `payment_channels`. `route_code` defaults to the client's
    `is_default` route for the method. A partial unique index
    guarantees at most one active match, so the protocol is never
    chosen by ordering; `priority` is informational for admin tooling
    and is not used to resolve a request.
4.  Confirm the merchant account is active and belongs to this
    deployment's environment.
5.  Read the protocol from the selected channel row. A route resolves to
    exactly one protocol.
6.  Load credentials from the environment using `credential_ref`
    (section 9.4).
7.  Persist the chosen merchant account, channel, protocol, and
    generated provider partner reference on the transaction **before**
    provider submission.
8.  Call the adapter driver for that protocol with the immutable
    selection.

The client cannot pass an arbitrary merchant account ID or protocol.

**No cross-protocol failover.** If a create call is ambiguous, the
gateway never retries it through the other protocol. Protocol switches
are administrator route changes performed only after the previous
outcome is known.

## 9. Faspay Adapter

### 9.1 Interface

Both drivers implement the same internal interface so the payment
service is protocol-agnostic:

-   `create_payment(ctx) -> CreateResult`
-   `inquire_payment(payment) -> InquiryResult`
-   `cancel_payment(payment) -> CancelResult` (if supported)
-   `verify_callback(request) -> VerifiedEvent`
-   `normalize_status(raw) -> NormalizedStatus`

The adapter owns request/response mapping, signing, timeouts (defaults:
15 s for create, 8 s for inquiry and Legacy callback verification,
versus 30 s in the existing integration; configurable and always below
the function `maxDuration`), safe error classification, provider reference
extraction, callback verification, and status normalization. Each log
line and metric carries `protocol`.

Generate `provider_partner_reference` in the payment service (compact,
random, unique per merchant account; 16 characters, the strictest
documented length) and pass it to the driver. It becomes
`partnerReferenceNo` / `trxId` (SNAP) or `bill_no` (Legacy). Confirm
Faspay's per-channel length limits.

### 9.2 SNAP driver

**Base URLs:** production `https://web.faspay.co.id`, sandbox
`https://debit-sandbox.faspay.co.id` (as in the existing integration).

**Request signing** (asymmetric, per Faspay Signature SNAP):

```
stringToSign = HTTPMethod + ":" + EndpointUrl + ":"
             + lowercase(hex(SHA256(minify(requestBody)))) + ":" + X-TIMESTAMP
signature    = base64( RSA-SHA256-sign(merchant_private_key, stringToSign) )
```

-   `EndpointUrl` is the relative path, including any URL parameters.
-   `X-TIMESTAMP` is ISO-8601 with offset (for example
    `2026-10-20T09:30:00+07:00`). Use the documented timezone
    (Asia/Jakarta in the existing code).
-   The body hashed must be byte-identical to the body sent (minified
    JSON). Serialize once and reuse the bytes.
-   Implement with the `cryptography` library using the PEM built from
    `{PREFIX}_PRIVATE_KEY` (the existing convention wraps the base64 key
    body in `RSA PRIVATE KEY` markers).

**Request headers:** `Content-Type: application/json`, `X-TIMESTAMP`,
`X-SIGNATURE`, `X-PARTNER-ID` (the merchant's partner ID), 
`X-EXTERNAL-ID` (unique within the day; use a random UUID),
`CHANNEL-ID` (the existing integration uses `77001`; confirm per
product).

**Operations**

  --------------------------------------------------------------------------------------------------
  Method     Create                                Inquiry                      Correlation keys
  ---------- ------------------------------------- ---------------------------- --------------------
  `VA`       `POST /v1.0/transfer-va/create-va`    `POST                        `virtualAccountNo`,
                                                   /v1.0/transfer-va/status`    `trxId`,
                                                                                `partnerServiceId`

  `QRIS`     `POST /v1.0/qr/qr-mpm-generate`       `POST                        `partnerReferenceNo`,
                                                   /v1.0/qr/qr-mpm-query`       `referenceNo`

  `EWALLET`  `POST                                 `POST /v1.0/debit/status`    `partnerReferenceNo`,
             /v1.0/debit/payment-host-to-host`                                  `referenceNo`
  --------------------------------------------------------------------------------------------------

Request field mapping (from the existing integration and Faspay docs):

-   **QRIS generate:** `partnerReferenceNo`, `amount{value "150000.00",
    currency}`, `merchantId`, `validityPeriod` (ISO-8601),
    `additionalInfo{billDate, billDescription, channelCode, phoneNo}`.
    Response: `referenceNo`, `partnerReferenceNo`,
    `additionalInfo.qrImageUrl`.
-   **QRIS query:** `originalReferenceNo`, `originalPartnerReferenceNo`,
    `serviceCode` (the Faspay docs give service code `51` for query; the
    existing code sends `47`, which the docs assign to generate;
    confirm), `merchantId`, `channelCode`. Response `latestTransactionStatus`.
-   **VA create:** map from the existing `buildVaBankRequest` (customer
    name, amount, expiry, `channelCode`). Response:
    `virtualAccountData.virtualAccountNo`, `expiredDate`,
    `additionalInfo.redirectUrl`. The request carries no VA number:
    Faspay assigns it (confirmed). `trxId` is the gateway's provider
    partner reference, not the consumer's reference as in the existing
    code.
-   **VA status:** `partnerServiceId`, `customerNo`, `virtualAccountNo`,
    `additionalInfo{channelCode, trxId}`. Response `paymentFlagStatus`.
-   **E-wallet:** `partnerReferenceNo`, amount, `merchantId`,
    `validityPeriod`, channel-specific items for DANA (`722`, `819`) and
    LinkAja (`716`). Response `webRedirectUrl`.

Amounts are sent as strings with two decimals (rupiah, for example
`"150000.00"`).

**SNAP notification specification**

Sources: Faspay SNAP Virtual Account, SNAP QRIS, SNAP Direct Debit, and
Signature SNAP pages. Where the pages are silent or inconsistent, the
item is marked **confirm in sandbox** and is also listed in section 20.

*Scope.* The MVP handles notifications for **dynamic VA** (created with
Create VA), QRIS, and e-wallet. Static VA notifications (`referenceNo`,
and the Faspay-to-merchant VA inquiry callback
`/v1.0/transfer-va/inquiry`) are out of scope.

*Routes.* Faspay posts `Content-Type: application/json` to the
registered merchant URL plus the SNAP path. Serve the paths at the
**root of the registered URL** (for example `url_merchant = https://host`)
to avoid signature-path ambiguity.

| Route | Faspay service | Payment method | Success code |
|---|---|---|---|
| `POST /v1.0/transfer-va/payment` | VA payment (service 25) | `VA` | `2002500` |
| `POST /v1.0/qr/qr-mpm-notify` | QRIS notify (service 52) | `QRIS` | `2005200` |
| `POST /v1.0/debit/notify` | Direct debit notify (service 56, from the documented sample `2005600`) | `EWALLET` | `2005600` |

Faspay documents `/v1.0/debit/notify` for e-wallet (Direct Debit). The
engineering team reports that e-wallet notifications arrive on the VA
path. All three routes therefore register the **same handler**. The
parser is chosen by payload shape, never by route or `channelCode`:

-   `virtualAccountNo` present: **VA shape**.
-   `originalPartnerReferenceNo` present: **reference shape** (shared by
    QRIS and e-wallet).
-   Anything else: `400` (`400SS02`), stored as `REJECTED`.

The payment method comes from the stored payment. The shape must be
compatible with it (VA shape with a `VA` payment; reference shape with a
`QRIS` or `EWALLET` payment). An incompatible pair is stored as
`UNMATCHED` and alerted, and is not applied. Confirm in sandbox which
route Faspay uses for e-wallet.

*Payload.* Amounts are strings with two decimals (rupiah, for example
`"12000.00"`).

| Item | VA shape | Reference shape (QRIS, e-wallet) |
|---|---|---|
| Merchant ID | `additionalInfo.merchantId` | `additionalInfo.merchantId` (QRIS) or top-level `merchantId` (e-wallet); read either |
| Our reference | `trxId` (String 16) = provider partner reference | `originalPartnerReferenceNo` (String 64) = provider partner reference |
| Provider reference | `virtualAccountNo` (28) and `paymentRequestId` (dynamic VA: equal to the VA number) | `originalReferenceNo` (64) |
| Other VA keys | `partnerServiceId` (8), `customerNo` (20) | none |
| Amount | `paidAmount.value`, `paidAmount.currency` | `amount.value`, `amount.currency` (the object is optional) |
| Status | `additionalInfo.latestTransactionStatus` (2) | `latestTransactionStatus` (2) |
| Status text | `additionalInfo.transactionStatusDesc` (optional) | `transactionStatusDesc` (optional) |
| Paid time, channel | `additionalInfo.paymentDate`, `additionalInfo.channelCode` | `additionalInfo.paymentDate`, `additionalInfo.channelCode` |

Mandatory fields: VA shape needs `partnerServiceId`, `customerNo`,
`virtualAccountNo`, `paymentRequestId`, `trxId`, and
`additionalInfo.{paymentDate, channelCode, merchantId,
latestTransactionStatus}`. Reference shape needs
`originalPartnerReferenceNo`, `originalReferenceNo`,
`latestTransactionStatus`, `additionalInfo.{paymentDate, channelCode}`,
and a merchant ID in one of the two locations. A missing mandatory field
returns `400SS02`; a malformed one returns `400SS01`.

`trxId` is 16 characters in the VA notification (32 in Create VA). The
gateway's 16-character provider partner reference fits both.

*Correlation.*

1.  Resolve the merchant account by `(environment, provider_merchant_id)`
    from the merchant ID.
2.  Find the payment by `provider_partner_reference` (VA `trxId`, or
    `originalPartnerReferenceNo`) within that merchant.
3.  If the payment has a stored `provider_reference`, it must equal the
    callback's provider reference (VA number or `originalReferenceNo`);
    a mismatch is `UNMATCHED` and alerted.
4.  If the stored `provider_reference` is null (the create response was
    lost), accept the callback after the amount check and fill it from
    the callback. This is how a lost VA create response is recovered.
5.  Never select the route, method, or client from `channelCode`. Faspay
    pairs more than one code with one wallet (for example `716` and
    `302` for LinkAja).

*Amount and currency.* Convert `value` to integer rupiah and require it
to equal the stored amount, and the currency to equal `IDR`. When the
amount object is absent (optional in the reference shape), the amount
check relies on the inquiry (`SIGNATURE_AND_INQUIRY`); such an event is
not applied in `SIGNATURE` mode.

*Verification (`verify_callback`).*

1.  Read the signature from `X-SIGNATURE_FROM_FASPAY` (the header name
    on the Faspay Signature SNAP page for Faspay-to-merchant messages),
    falling back to `X-SIGNATURE`, and read `X-TIMESTAMP`. The pages do
    not document notification headers, and some proxies drop header names
    containing underscores, so confirm in sandbox that the header
    reaches the function.
2.  Build `stringToSign` with the same formula as requests:
    `POST:` + the request path + `:` +
    `lowercase(hex(SHA256(minified raw body)))` + `:` + the received
    timestamp.
3.  Base64-decode the signature and verify it with SHA256withRSA against
    the **Faspay public key** (`{PREFIX}_FASPAY_PUBLIC_KEY`, or a shared
    environment-level key).
4.  Enforce a timestamp tolerance (configurable) and use the persisted
    event fingerprint for replay protection.
5.  Resolve the merchant, then the payment, then validate amount,
    currency, and correlation as above before applying.

`CALLBACK_VERIFY_MODE` is configurable: `SIGNATURE_AND_INQUIRY` (the
default until the section 20 release gates close) or `SIGNATURE`. In
`SIGNATURE_AND_INQUIRY`, a callback that would move a payment to
`SUCCEEDED` is confirmed by a SNAP inquiry before the transition: VA
uses `POST /v1.0/transfer-va/status` (`partnerServiceId`, `customerNo`,
`virtualAccountNo`, `additionalInfo.channelCode`,
`additionalInfo.trxId`), QRIS uses `qr-mpm-query`, and e-wallet uses
`/v1.0/debit/status`. Switch to `SIGNATURE` only after sandbox
confirmation, and keep `SIGNATURE_AND_INQUIRY` as a safety option
afterwards. If the notification signature cannot be verified for any
reason (for example the header does not arrive), treat the notification
only as a hint and require the inquiry result; the event is stored with
`verification_method = SNAP_INQUIRY_ONLY` and `signature_valid = false`.

*Status mapping.* The notification pages document only
`latestTransactionStatus = 00`; the other values come from the inquiry
tables and are mapped by section 9.5. Any value outside that table is
recorded in `provider_status`, alerted, and not applied.

*Acknowledgement.* Reply with `Content-Type: application/json` and an
`X-TIMESTAMP` header (ISO-8601 with offset), and the SNAP body. In the
codes, `SS` is the service code (25 VA, 52 QRIS, 56 e-wallet).

-   **Success (HTTP 200).** QRIS:
    `{"responseCode": "2005200", "responseMessage": "Request has been processed successfully"}`.
    E-wallet: the same with `2005600`. VA: `2002500` and `"Successful"`,
    **plus** a `virtualAccountData` object echoing `partnerServiceId`,
    `customerNo`, `virtualAccountNo`, `paymentRequestId`, and
    `paidAmount` (`value`, `currency`).
-   **Invalid signature:** HTTP `401`, `401SS00`.
-   **Missing or malformed field:** HTTP `400`, `400SS02` or `400SS01`.
-   **Unmatched** (section 11.2): VA `4042512` (invalid bill / VA);
    QRIS and e-wallet `404SS01` (transaction not found).
-   **Invalid amount:** `404SS13`; **paid bill:** `404SS14`; **expired:**
    `404SS19` (or `403SS00`, "Transaction Expired"). These codes are
    documented for VA only; the QRIS and e-wallet equivalents are
    inferred.
-   **Duplicate of a `PROCESSED` event:** return the success body again
    (a duplicate of an `UNMATCHED` event is re-correlated, section
    10.3). Faspay documents `409` (`409SS01`, "Duplicate
    partnerReferenceNo") for a previously processed transaction; the
    gateway deliberately answers success so that a lost acknowledgement
    cannot leave Faspay retrying. Confirm in sandbox.
-   **Gateway error:** HTTP `500`, `500SS01`, so Faspay may retry.

Retry behavior is not documented on any of the three pages. The
"not found" reply for unmatched events assumes Faspay retries (section
11.2); confirm in sandbox.

*Documentation quirks to remember.* All three pages describe
`channelCode` as "VA ONLY" (a copy-paste leftover; the e-wallet sample
uses `713`), `merchantId` is String(5) in the reference shape, the
published sample JSON contains typos, and the status tables disagree on
the meaning of `01` and `07`.

### 9.3 Legacy driver

Used for `RETAIL` (Alfagroup `707`, Indomaret `706`) and as an optional
fallback driver for other channels when a route points to a `LEGACY`
channel row.

**Base URLs:** production `https://web.faspay.co.id`, sandbox
`https://debit-sandbox.faspay.co.id`.

**Signature (legacy):** `sha1(md5(user_id + password + bill_no))`, as in
the existing PHP and Python code. Credentials come from the environment
(section 9.4).

**Create (Post Data Transaction):** `POST /cvr/300011/10` with JSON:
`request` ("Post Data Transaction"), `merchant_id`, `merchant`,
`bill_no` (the provider partner reference), `bill_reff` (also the
provider partner reference, never the consumer's reference), `bill_date`,
`bill_expired`, `bill_desc`, `bill_currency` (`IDR`), `bill_gross`
(`"0"`), `bill_miscfee` (`"0"`), `bill_total` (rupiah x 100),
customer fields (`cust_no`, `cust_name`, `msisdn`, `email`),
`payment_channel` (`707` / `706`), `pay_type`, `terminal`, `item`,
`signature`. Response: `trx_id` (the payment code for retail),
`response_code` (`00` success), `response_desc`, `redirect_url`
(Faspay page that shows the payment code, expiry, and instructions).
Dates use Jakarta local time (the legacy code adds +7 hours to UTC).

**Inquiry:** `POST /cvr/100004/10` with `request` ("Inquiry Status
Payment"), `trx_id`, `merchant_id`, `bill_no`, `signature`; response
`payment_status_code`, `bill_no`, `response_code`.

**Legacy callback ingress:** `POST
/v1/webhooks/faspay/legacy/notification`.

-   Inbound fields (sample from Faspay): `request` ("Payment
    Notification"), `trx_id`, `merchant_id`, `merchant`, `bill_no`,
    `payment_reff`, `payment_date` (`Y-m-d H:i:s`),
    `payment_status_code`, `payment_status_desc`, `bill_total`,
    `payment_total`, `payment_channel_uid`, `payment_channel`, and
    `signature`. The existing handler (`api-ki-cf-v4`,
    `CntFaspayController@callback`) requires only `request`, `trx_id`,
    `merchant_id`, and `bill_no`; the gateway requires the same four.
-   Amounts use the create format (rupiah x 100): the sample
    `bill_total` and `payment_total` of `"1000"` mean Rp 10. Compare
    `payment_total / 100` with the stored amount before applying.
-   In the sample, `payment_channel_uid` `302` is `LinkAja`. The SNAP
    Direct Debit channel list also pairs codes for one wallet (`716` /
    `302` for LinkAja, `819` / `722` for DANA), so one wallet can carry
    two codes. Seed the channel catalogue accordingly, and correlate by
    the stored payment, never by `payment_channel` or `channelCode`.
-   The existing implementation **ignores the received signature** and
    confirms through the inquiry API. The gateway keeps this as the
    authoritative control (`verification_method = LEGACY_INQUIRY`):
    never mark a payment `SUCCEEDED` from the notification body alone.
    Recomputing and comparing the notification signature is an
    additional check to add once Faspay's notification signature
    formula is confirmed.
-   Resolve the merchant account from `merchant_id` and the payment from
    `bill_no` (= `provider_partner_reference`) and/or `trx_id`
    (= `provider_reference`) within that merchant.
-   Faspay retries the notification up to three times when it does not
    receive an acceptable response, so the reply must be correct and
    fast.
-   Response (per the existing implementation): `response`
    ("Payment Notification"), `trx_id`, `merchant_id`, `merchant`,
    `bill_no`, `response_code`, `response_desc`, `response_date`
    (`Y-m-d H:i:s`). `merchant`, `response_code`, and `response_desc`
    are copied from the inquiry response, so the reply carries the
    inquiry's own code. If the inquiry itself fails, the existing code
    returns the inquiry error object; the gateway instead replies with a
    non-success Legacy response so Faspay retries.

**Legacy redirect.** The `GET faspay-callback` customer redirect is a
consumer-side page and is out of scope for the gateway.

### 9.4 Credentials (environment variables)

Secrets are read from the environment (Vercel project variables). The
database stores only the prefix in `merchant_accounts.credential_ref`.
Per merchant account `{PREFIX}`:

  -----------------------------------------------------------------------
  Variable                       Used by    Purpose
  ------------------------------ ---------- -----------------------------
  `{PREFIX}_PRIVATE_KEY`         SNAP       RSA private key (base64 body;
                                            existing convention)

  `{PREFIX}_FASPAY_PUBLIC_KEY`   SNAP       Faspay public key for
                                            callback verification

  `{PREFIX}_LEGACY_USER`         Legacy     Faspay legacy user ID

  `{PREFIX}_LEGACY_PASSWORD`     Legacy     Faspay legacy password
  -----------------------------------------------------------------------

Non-secret identifiers (`partner_id`, `provider_merchant_id`,
`merchant_name`) are database columns. Global variables include
`APP_ENV`, `DATABASE_URL` (Neon pooled), the OAuth signing key pair
(`OAUTH_JWT_PRIVATE_KEY`, `OAUTH_JWT_PUBLIC_KEY`), `ADMIN_API_TOKEN_HASH`,
`QSTASH_CURRENT_SIGNING_KEY`, `QSTASH_NEXT_SIGNING_KEY`, `QSTASH_TOKEN`, `CALLBACK_VERIFY_MODE`, retry and reconciliation
settings, and per-client webhook secrets (`webhook_secret_ref` is the
full variable name; `<name>_PREV` optionally holds the previous secret
during rotation). Never log these values; fail fast at startup (or
first use) with a clear error when a referenced variable is missing.
Vercel environment variables are scoped per environment, which
supports sandbox/production isolation.

### 9.5 Status mapping

Mapping lives in `adapters/faspay/status_map.py`, one table per
protocol, covered by unit tests. Raw status is always stored in
`provider_status`.

**SNAP** (`latestTransactionStatus` / `paymentFlagStatus`):

  -----------------------------------------------------------------------
  Raw     Meaning (Faspay docs)   Normalized
  ------- ----------------------- ---------------------------------------
  `00`    Success                 `SUCCEEDED`

  `01`    Initiated (callback) /  `PENDING`
          Paying (VA inquiry)     

  `02`    Paying (callback list)  `PENDING`

  `03`    Pending                 `PENDING`

  `04`    Refunded                Recorded; status unchanged; alert for
                                  manual review (refund is out of scope)

  `05`    Canceled                `CANCELLED`

  `06`    Failed                  `FAILED`

  `07`    Expired (also listed    `EXPIRED` (resolve the "not found"
          as "Not found")         ambiguity with Faspay)
  -----------------------------------------------------------------------

**Legacy** (`payment_status_code`; confirm full table with Faspay):

  -----------------------------------------------------------------------
  Raw     Meaning                 Normalized
  ------- ----------------------- ---------------------------------------
  `2`     Success (used by the    `SUCCEEDED`
          existing code)          

  `0`, `1` Unprocessed / in       `PENDING`
          process                 

  `3`     Failed                  `FAILED`

  `7`     Expired                 `EXPIRED`

  `8`     Cancelled               `CANCELLED`

  `4`     Reversal (like SNAP    Recorded; status unchanged; alert
          `04`)                   for manual review

  `5`, `9`  Not found /           Recorded; status unchanged; alert
          unknown (confirm        and enqueue `INQUIRE_PAYMENT`
          meanings)
  -----------------------------------------------------------------------

Only `2` is confirmed by the existing code. The other legacy values are
recorded from Faspay's legacy conventions and must be verified.

## 10. Idempotency

### 10.1 API request idempotency

-   Unique key: `(client_id, idempotency_key)`.
-   Store the canonical request fingerprint and operation state.
-   Same key and same fingerprint returns the existing operation/result.
-   Same key and different fingerprint returns `409 Conflict`.
-   Concurrent requests with the same key are serialized by the unique
    constraint (`INSERT ... ON CONFLICT DO NOTHING`, then read).
-   Do not expire an unresolved operation.
-   Do not assume internal idempotency guarantees provider-side
    idempotency.
-   Additionally only one payment outside `FAILED`, `EXPIRED`, and
    `CANCELLED` may exist per `(client_id, merchant_reference)` (partial
    unique index). After a final failure or expiry, a request with a new
    `Idempotency-Key` creates a new payment; the same key keeps
    returning the original result.

### 10.2 Ambiguous create result

If a request times out after submission:

1.  Mark the attempt `UNKNOWN` and the payment `UNKNOWN`.
2.  Do not blindly resubmit a create operation, and never through the
    other protocol.
3.  Enqueue an `INQUIRE_PAYMENT` job: SNAP status/query endpoint or
    Legacy inquiry by `bill_no`, using the stored provider partner
    reference. For SNAP `VA`, the inquiry needs the Faspay-assigned
    `virtualAccountNo`, which is unknown if the create response was
    lost. In that case no inquiry is possible: the payment stays
    `UNKNOWN`, and recovery relies on the payment callback (correlated
    by `trxId` = the provider partner reference) or on manual
    reconciliation.
4.  If the outcome remains unknown, schedule reconciliation and expose
    `UNKNOWN` to the consumer.
5.  Retry create only when the prior operation is proven not to have
    created a payment, or provider semantics guarantee safe
    idempotency.

### 10.3 Webhook idempotency

-   Compute the protocol-specific fingerprint (ERD 3.8) and insert with a
    unique constraint (`ON CONFLICT DO NOTHING`).
-   Lock/conditionally update the payment transaction.
-   Validate state transition and provider evidence.
-   Insert the `payment_events` row and the delivery and job rows in the
    same database transaction.
-   A duplicate of a `PROCESSED` event returns the protocol's success
    acknowledgement without repeating business effects. A duplicate of
    an `UNMATCHED` event is re-correlated and answered with the new
    result.
-   Only verified events take the unique fingerprint. `REJECTED` events
    (failed verification) are stored with a random fingerprint salt, so
    a forged payload cannot occupy a genuine event's fingerprint.

## 11. Webhook Routing and Processing

### 11.1 Processing

Because the transition work is small, a matched, verified callback is
**applied inside the ingress request** in one database transaction. Only
consumer delivery is asynchronous.

1.  Receive callback on the protocol's ingress route.
2.  Verify authenticity (SNAP signature, or Legacy inquiry). On
    failure, store a `REJECTED` event (rate-limited, retention-purged)
    and reply with the protocol's rejection response.
3.  Parse only the required fields.
4.  Resolve the merchant account, then the payment (provider references
    within that merchant scope).
5.  Validate merchant, amount, currency, and identifiers.
6.  In one transaction: insert the inbox event (dedupe), apply the
    guarded state transition, mark the event processed, insert the
    `payment_events` row, then the `webhook_deliveries` row and
    `DELIVER_WEBHOOK` job.
7.  Commit, then acknowledge in the protocol's required format.
8.  Best-effort nudge (a QStash publish) to run the job soon; the
    scheduled run is the safety net.

### 11.2 Unmatched events

-   A verified but unmatched event is persisted (`UNMATCHED`, no
    `payment_id`) and a `RETRY_CORRELATION` job plus a reconciliation
    job are scheduled.
-   The ingress then responds with the protocol's "not found" style
    reply rather than an unconditional success, so that Faspay may retry
    while the create response is still being persisted. This is a design
    assumption to confirm against Faspay's retry behavior; if Faspay
    does not retry, the persisted event plus reconciliation provide
    recovery. A Faspay retry of the same event is re-correlated
    (section 10.3) and answered with the new result, not with a blind
    success.
-   Never guess the target client using amount, timestamp, or
    free-form payload fields.
-   Provide an authorized operational workflow for investigation and
    replay.

### 11.3 State transitions

Normalized statuses: `CREATED`, `PENDING`, `SUCCEEDED`, `FAILED`,
`EXPIRED`, `CANCELLED`, `UNKNOWN`.

Implement an explicit state machine in `services/state_machine.py`.
The allowed transitions are normative:

| From | To |
|---|---|
| `CREATED` | `PENDING`, `FAILED`, `UNKNOWN` |
| `PENDING` | `SUCCEEDED`, `FAILED`, `EXPIRED`, `CANCELLED`, `UNKNOWN` |
| `UNKNOWN` | `PENDING`, `SUCCEEDED`, `FAILED`, `EXPIRED`, `CANCELLED` |

`SUCCEEDED`, `FAILED`, `EXPIRED`, and `CANCELLED` are final and do not
change. A late or contradictory callback must not overwrite a final
state without provider evidence and a defined rule; such cases raise an
alert and enqueue reconciliation.

Every applied transition inserts a `payment_events` row (ERD 3.13) in
the same transaction, with `event_type` `payment.<status in lowercase>`.
A delivery and a `DELIVER_WEBHOOK` job are created for
`payment.succeeded`, `payment.failed`, `payment.expired`,
`payment.cancelled`, and `payment.unknown`. `payment.created` and
`payment.pending` are recorded but not delivered by default.

## 12. Background Jobs: Database Outbox, SKIP LOCKED, QStash

### 12.1 Principles

-   `gateway_jobs` is the source of truth for all background work:
    consumer delivery, correlation retry, provider inquiry,
    reconciliation, expiry, and retention purge.
-   Workers are **bounded, triggered executions**, not continuous
    processes and not polling loops.
-   Concurrent runs are safe: rows are claimed with `FOR UPDATE SKIP
    LOCKED`; leases recover rows from crashed runs; handlers are
    idempotent.
-   Queue payloads carry IDs, not sensitive payment data.

### 12.2 Triggers

-   **Upstash QStash schedules** call `POST /internal/jobs/run`
    periodically (for example every minute to claim delivery and inquiry
    jobs, plus the scan runs of section 12.7). This replaces
    Vercel cron entirely; do not depend on frequent Vercel Hobby Cron.
-   **Post-commit nudge:** after enqueuing urgent work (a delivery),
    publish a one-off QStash message to `/internal/jobs/run` (best
    effort). The next scheduled run covers a missed nudge.
-   QStash requests are authenticated by verifying the
    `Upstash-Signature` header (signed JWT) with
    `QSTASH_CURRENT_SIGNING_KEY` and `QSTASH_NEXT_SIGNING_KEY`, and
    checking the body hash and target URL claim. Reject everything else
    with `401`. These routes are not part of the client API.
-   A run accepts bounds (`max_jobs`, `time_budget_ms`, optional
    `job_types`, optional `scan`), capped by server-side maximums.

### 12.3 Claim query (raw SQL)

``` sql
WITH picked AS (
    SELECT id
    FROM gateway_jobs
    WHERE (
            status IN ('PENDING', 'RETRY')
            AND run_after <= now()
          )
       OR (
            status = 'RUNNING'
            AND locked_until < now()
          )
    ORDER BY run_after, id
    LIMIT :batch_size
    FOR UPDATE SKIP LOCKED
)
UPDATE gateway_jobs j
SET status       = 'RUNNING',
    attempts     = j.attempts + 1,
    locked_by    = :run_id,
    locked_until = now() + make_interval(secs => :lease_seconds),
    updated_at   = now()
FROM picked
WHERE j.id = picked.id
RETURNING j.*;
```

-   The claim is one short transaction; commit before executing any
    job.
-   Execute claimed jobs one at a time or with small bounded
    concurrency, watching the time budget.
-   Finish: `DONE` with `finished_at`; or `RETRY` with a new
    `run_after`; or `DEAD` after `max_attempts` (also mark the related
    delivery `DEAD_LETTER` and raise an alert).
-   Use a `RETURNING` clause and `WHERE status = 'RUNNING' AND locked_by
    = :run_id` when completing, so a run that lost its lease cannot
    overwrite a newer run.
-   Priority index: `gateway_jobs(status, run_after)`.

### 12.4 Retry and backoff

-   Delay = `min(cap, base * 2^(attempts-1))` plus random jitter.
    Illustrative defaults: base 60 s, cap 6 h, jitter up to 20%, max
    attempts 8 for deliveries (about 1, 2, 4, 8, 16, 32 minutes and
    beyond). All values are configuration, not a Faspay requirement.
-   Retry transient network errors, timeouts, HTTP `408`, `429`, and
    `5xx`. Treat other `4xx` as non-retryable unless a documented rule
    says otherwise. Honor `Retry-After` when present.
-   After the limit: `DEAD` / `DEAD_LETTER`, alert, and a replay
    operation for operators.
-   Create-payment is **not** retried by the job runner. Only
    inquiry-based resolution runs in the background (section 10.2).

### 12.5 Job types

  -----------------------------------------------------------------------
  Job type              Purpose
  --------------------- -------------------------------------------------
  `DELIVER_WEBHOOK`     Send the signed event to the client endpoint

  `RETRY_CORRELATION`   Re-try matching an unmatched event after the
                        create response was persisted

  `INQUIRE_PAYMENT`     Provider inquiry for `UNKNOWN` creates, or to
                        confirm a success (protocol-specific)

  `RECONCILE_PAYMENT`   Compare gateway state with provider state for
                        stale `PENDING`, `UNKNOWN`, or discrepancies

  `EXPIRE_PAYMENT`      Move payments past `expires_at` through
                        inquiry-confirmed expiry

  `PURGE_RETENTION`     Delete or archive rows beyond retention to keep
                        the database small
  -----------------------------------------------------------------------

### 12.6 Delivery handler

-   Re-read the delivery, payment event, and payment inside the handler; check
    that it is not already `DELIVERED` (idempotency).
-   Validate the destination at send time: HTTPS only, resolved IPs not
    private/loopback/link-local/metadata, redirects not followed, short
    timeout, response size cap.
-   Sign and send; record `last_http_status`, error category, and
    attempt count; mark `DELIVERED` on 2xx.
-   The consumer-facing guarantee is **at-least-once**.

### 12.7 Scheduled scans and proposed defaults

Scans create the recurring job rows; nothing else enqueues them.

-   A QStash schedule calls `/internal/jobs/run` with `"scan": true`
    (every 5 minutes) before the run claims jobs. The scan inserts
    `INQUIRE_PAYMENT`, `RECONCILE_PAYMENT`, and `EXPIRE_PAYMENT` rows,
    deduplicated by `dedupe_key` (for example
    `reconcile:{payment_id}:{hour_bucket}`). A daily scan inserts
    `PURGE_RETENTION`.
-   Proposed defaults, all configuration and to be agreed:

| Item | Default |
|---|---|
| `UNKNOWN` payment | `INQUIRE_PAYMENT` enqueued at the timeout with `run_after` = now + 2 minutes, then exponential backoff |
| Stale `PENDING` past `expires_at` | QRIS and EWALLET 15 minutes; VA 1 hour; RETAIL 6 hours |
| Function `maxDuration` | 60 s (confirm against the Vercel plan) |
| Provider timeout | 15 s create; 8 s inquiry |
| Status and route cache TTL | 30 s |
| Retention | See ERD section 6 |

## 13. Reconciliation

Schedule reconciliation (through `INQUIRE_PAYMENT` / `RECONCILE_PAYMENT`
jobs) for:

-   Payments in `UNKNOWN`.
-   Payments `PENDING` beyond configured thresholds (per method; retail
    codes live longer than QR).
-   Unmatched callback events.
-   Missing or delayed callbacks.
-   Potential status or amount discrepancies.

Use the SNAP inquiry endpoints or the Legacy inquiry
(`/cvr/100004/10`) according to the payment's stored protocol. Apply
rate limits and stagger jobs. If an automated inquiry is unavailable for
a merchant/method, provide a controlled manual/report-based workflow.
Report counts per protocol.

## 14. Database Access and Transaction Boundaries

-   Runtime access is raw SQL via `sqlalchemy.text()` with bound
    parameters, in `repositories/`. Never build SQL by string
    formatting.
-   ORM models and Alembic migrations define the schema only.
-   Use one connection and one short transaction per unit of work.

For a valid callback event:

1.  Begin database transaction.
2.  Insert the inbox event (dedupe by fingerprint).
3.  Lock the payment row (`SELECT ... FOR UPDATE`) or use a conditional
    update.
4.  Validate the event and state transition.
5.  Update payment state/provider status.
6.  Mark the inbox event processed.
7.  Insert the `payment_events` row, then the delivery and job rows.
8.  Commit.
9.  Acknowledge the provider.

For create:

1.  Authenticate and validate.
2.  Resolve the route (cached lookup).
3.  Claim the idempotency key.
4.  Insert the payment (including generated provider partner reference),
    the attempt record, and the `payment.created` event.
5.  Commit the pre-submission state.
6.  Call the provider **outside** the database transaction.
7.  Persist the response/outcome and identifiers, with the resulting
    `payment_events` row.
8.  Resolve any early callback that arrived before provider references
    were saved (`RETRY_CORRELATION`).

Do not hold a database transaction open across a provider network call.

## 15. Security Requirements

-   TLS for all service communication.
-   OAuth client authentication and per-client authorization.
-   Provider credentials in environment variables; database stores
    references only; secrets never appear in logs, errors, or audit
    records.
-   SNAP callback verification with the Faspay public key; Legacy
    callbacks confirmed by inquiry.
-   Signed gateway-to-consumer callbacks with timestamp/replay
    protection.
-   QStash request signature verification on every internal job route.
-   Destination URL allow-listing, HTTPS enforcement, DNS/IP validation,
    and private-network/metadata-address blocking to prevent SSRF.
-   Rate limits per client and endpoint (Vercel Firewall rules in the
    MVP).
-   Environment isolation for sandbox and production (separate Vercel
    environments, Neon databases/branches, and variables).
-   Redaction of tokens, signatures, credentials, and unnecessary
    personal data in logs.
-   Admin and replay operations require the admin token
    (`ADMIN_API_TOKEN_HASH`) and are audited with `actor_type = ADMIN`.
-   Audit log for merchant route changes, credential reference changes,
    endpoint changes, and manual status/replay actions.
-   Database backups (Neon point-in-time restore availability depends on
    the plan) and a tested restore procedure.
-   Least-privilege database role (the application role cannot alter
    schema).

## 16. Observability and Operations

### Metrics (all labelled with `protocol` and `merchant_code`)

-   Payment create success/failure/unknown rate.
-   Provider latency and error categories.
-   Callback received/verified/rejected/unmatched counts.
-   Oldest due job age and pending job count (the key signal for the
    scheduler).
-   Delivery success rate, retry count, and dead-letter count.
-   Reconciliation discrepancies.
-   Request rate and latency per endpoint class (for the 500 rps
    target).

### Structured log fields

-   `correlation_id`
-   `protocol`
-   `payment_id`
-   `client_id`
-   `merchant_account_id`
-   `payment_attempt_id`
-   `webhook_event_id`
-   `delivery_id`
-   `job_id`
-   operation, duration, outcome, and safe error category

Do not log secrets or full sensitive payloads. Use `structlog` as in
`ki-cf-v5`; ship logs through Vercel's log drains or the chosen
platform. Define alerts for rising unknown outcomes, invalid callback
spikes, unmatched event growth, oldest-due-job age, and repeated
delivery failures. Alert if no job run has occurred recently (scheduler
failure).

## 17. Testing Strategy

### Unit tests

-   Route and channel selection; protocol resolution.
-   Request validation and normalization.
-   Idempotency fingerprint and conflict behavior.
-   State machine transitions.
-   Per-protocol status mapping.
-   SNAP signature construction and verification (known test vectors).
-   Legacy `SHA1(MD5)` signature.
-   Backoff calculation and jitter bounds.
-   OAuth token issue and validation.

### Integration tests

-   Sandbox create and inquiry for each relevant merchant, method, and
    **protocol separately**.
-   Provider timeout and unknown outcome.
-   Callback verification and correlation (SNAP VA, SNAP QRIS, SNAP
    e-wallet on both routes, Legacy).
-   Database uniqueness constraints.
-   Transactional inbox/outbox behavior.
-   Concurrent job claiming: two parallel runs never process the same
    job; lease expiry reclaims a crashed run's job.
-   Job retry, backoff, and dead-letter behavior.

### Failure and security tests

-   Duplicate callback.
-   Out-of-order callback.
-   Callback arrives before create response persistence.
-   Invalid signature and replay attempt.
-   Amount/currency/merchant mismatch.
-   Unknown provider reference.
-   A callback whose payload shape does not match the stored payment
    method is `UNMATCHED` and not applied.
-   Faspay retry of an `UNMATCHED` event is re-correlated, not
    acknowledged blindly.
-   A forged (rejected) payload does not block the identical genuine
    callback.
-   Create after a `FAILED` or `EXPIRED` payment with the same
    `merchant_reference` succeeds; after a `SUCCEEDED` one it conflicts.
-   Consumer endpoint timeout, `429`, and `5xx`.
-   Unauthorized cross-client transaction access.
-   Forged QStash request.
-   Malicious callback destination/SSRF attempt.
-   Function timeout after database commit but before dispatch.
-   Database interruption and recovery.
-   Two clients with the same `merchant_reference` on a shared merchant
    account.

### End-to-end and load tests

-   One shared merchant used by Fluency Hub and Crowdfunding.
-   Dedicated merchant routes.
-   Sandbox/production route isolation.
-   Consumer receives and verifies a signed callback.
-   Reconciliation resolves a deliberately ambiguous test transaction.
-   Load test toward 500 rps (section 4.1).

## 18. Migration and Rollback

### Phase 1: Inventory

-   Map Fluency Hub and Crowdfunding integrations, provider
    credentials, merchant accounts, channel codes, protocols in use,
    callback URLs, methods, and in-flight transactions.
-   Capture the actual callback payloads and provider reference fields
    for both protocols.
-   Map existing identifiers (`id_transaksi`, `id_donasi`, `trx_id`) to
    `merchant_reference` and `provider_reference`.
-   Define per-merchant cutover windows. The existing Legacy callback is
    registered per tenant (`/api/{lembaga}/faspay-callback`, with a
    separate Faspay credential per tenant); cutover replaces it per
    merchant with the gateway ingress URL.

### Phase 2: Build and sandbox

-   Implement registry, OAuth, routing, payment API, both ingress
    routes, outbox, job runner, and reconciliation.
-   Test each merchant configuration and failure scenario.
-   Run an early load test.

### Phase 3: Pilot

-   Choose one low-risk application and merchant.
-   Route new payment creation through the gateway.
-   Register the gateway callback endpoint for the relevant provider
    configuration (SNAP merchant URL and Legacy notification URL are
    separate).
-   Retain old callback handling for in-flight transactions where
    possible.
-   Reconcile gateway, consumer application, and Faspay records.

### Phase 4: Incremental rollout

-   Migrate one application at a time.
-   Verify callback routing and status reconciliation before proceeding.
-   Do not dual-submit real create-payment requests.
-   Keep a rollback plan that preserves transaction records and
    callback visibility.

### Phase 5: Decommission

-   Disable the old direct create integration after all clients migrate.
-   Remove obsolete callbacks only when in-flight transactions and old
    notification paths are accounted for.
-   Revoke old credentials where appropriate.
-   Keep reconciliation and audit records.

Rollback must not delete payment records or assume that reverting
application code cancels provider-side payments.

## 19. Deployment Configuration

Separate by environment:

-   Vercel project/environment, Neon database or branch, and variables.
-   Provider base URL and merchant credentials.
-   Callback ingress URLs (SNAP and Legacy are different).
-   OAuth signing keys and QStash keys.
-   Consumer webhook secrets.
-   Rate-limit, retry, lease, and reconciliation settings.
-   QStash schedules (job-run cadence) per environment.

Provide `/health` (liveness) and a readiness check that tests the
database connection. Plan the Alembic migration step in CI before
deploying. Keep `vercel.json` free of frequent cron entries.

## 20. Release Gates and Open Items

This is the single list of open technical items. Confirmed by the
engineering team (20 October 2026): Faspay assigns the VA number (the
create request carries none), and the merchant ID is the same value for
SNAP and Legacy.

Before production:

-   Exact SNAP notification headers, signature path form, and retry
    policy are confirmed in sandbox (the reviewed Faspay pages do not
    list notification headers; the Signature SNAP page names
    `X-SIGNATURE_FROM_FASPAY` for Faspay-to-merchant signatures).
-   The e-wallet notification route is confirmed: Faspay documents
    `/v1.0/debit/notify`, the engineering team reports the VA path, and
    the gateway accepts both. The `X-SIGNATURE_FROM_FASPAY` header
    reaches the function, the e-wallet payload matches the documented
    reference shape, and the `409` duplicate and retry behavior are
    confirmed.
-   Status mapping ambiguities (`07`, `01`/`02`) are resolved for SNAP;
    the full legacy `payment_status_code` table and the legacy
    notification signature are confirmed, and so is the reply code (the
    existing app echoes the inquiry's `response_code`).
-   Callback registration scope (per merchant, environment, or other) is
    confirmed; correlation and uniqueness scope of provider references
    is confirmed for both protocols.
-   Channel codes and enabled methods are confirmed per merchant
    (including whether Alfamart and Alfamidi are covered by `707`).
-   Whether Faspay requires outbound IP allow-listing is confirmed;
    static egress is arranged if so.
-   `serviceCode` for the QRIS query is confirmed (documentation says
    `51`; the existing code sends `47`).
-   Shared and dedicated merchant routes pass sandbox tests for both
    protocols.
-   Idempotency and ambiguous create outcomes are tested.
-   Durable inbox and transactional outbox are tested, including
    `SKIP LOCKED` concurrency.
-   QStash schedules and signature verification are in place and
    monitored.
-   Consumer signature verification is implemented by Fluency Hub and
    Crowdfunding.
-   Load test result against 500 rps is recorded with limits and
    scaling path.
-   Reconciliation and operational alerts are available.
-   Cutover/rollback plan is approved.
-   Finance/operations validate pilot transaction reconciliation.
-   The SNAP VA notification echoes `trxId` reliably, because a lost VA
    create response can only be correlated by it.
-   Which Legacy channel codes exist for VA and e-wallet if the Legacy
    driver is used as a fallback.
-   QStash free-tier message quota is sufficient for the post-commit
    nudge volume at the target load.
-   Default and maximum `expires_at` per channel (existing integrations:
    5 hours for SNAP, 1 day for Legacy), availability SLO, and
    event-retention and delivery SLAs are agreed.
-   How SNAP-created payments are notified today. `api-ki-cf-v4`
    creates VA, QRIS, and e-wallet payments through SNAP but has no SNAP
    notification handler: its only callback route is the Legacy-format
    `faspay-callback`, and SNAP status is read through the SNAP inquiry
    endpoint. Confirm in sandbox whether Faspay sends Legacy-format or
    SNAP-format notifications for SNAP-created payments, and which
    reference they carry (`bill_no` versus `partnerReferenceNo` /
    `trxId`). The SNAP ingress has no production-proven reference
    implementation until then.
