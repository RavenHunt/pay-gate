# Product Requirements Document (PRD)

## Faspay Payment Gateway Orchestrator (SNAP + Legacy)

-   **Document status:** Draft for review (brainstorming)
-   **Version:** 1.0
-   **Date:** 12 October 2026 (last revised 20 October 2026)
-   **Product type:** Internal payment integration platform
-   **Architecture direction:** FastAPI (async) on Vercel serverless,
    Neon PostgreSQL, database outbox with externally triggered bounded
    job runs
-   **Related documents:** `TRD-Faspay-Payment-Gateway-Orchestrator.md`,
    `ERD-Faspay-Payment-Gateway-Orchestrator.md`

## 1. Executive Summary

The organization has multiple applications that integrate directly with
Faspay. Faspay callback configuration is constrained to registered
webhook endpoints, while the organization needs one Faspay integration
to serve multiple applications and potentially multiple merchant
accounts.

The Faspay Payment Gateway Orchestrator centralizes payment creation,
transaction mapping, callback ingestion, callback routing, status
normalization, retries, and reconciliation. Applications integrate with
one internal gateway API instead of implementing Faspay-specific logic
independently.

Faspay exposes **two distinct integration protocols**, and the gateway
supports both behind one normalized API:

-   **SNAP** (Bank Indonesia national open API standard): asymmetric
    `SHA256withRSA` signatures, ISO-8601 timestamps, `X-PARTNER-ID` /
    `X-EXTERNAL-ID` / `CHANNEL-ID` headers, and 7-character response
    codes. Used for Virtual Account (VA), QRIS, and e-wallet direct
    debit.
-   **Legacy Faspay API** ("Post Data Transaction", `bill_no` /
    `trx_id` model): JSON with `SHA1(MD5(user_id + password + bill_no))`
    signatures and an inquiry-based verification pattern. Used for
    retail outlets (Alfagroup, Indomaret) and available as a
    configurable fallback driver for other methods.

Consumers never see which protocol served a payment; the protocol is an
internal, per-route attribute visible to operators only.

The gateway must support a mixed merchant model: some applications share
a Faspay merchant account, while others use dedicated accounts.

## 2. Problem Statement

-   Applications currently call Faspay directly, duplicating provider
    integration logic (SNAP signing, legacy signing, callback handling).
-   Registered Faspay callback URLs cannot directly notify every
    consuming application independently.
-   The gateway needs a reliable way to determine which application owns
    a transaction.
-   Callback delivery can be duplicated, delayed, or fail because a
    consumer application is unavailable.
-   A timeout during payment creation may leave the transaction outcome
    unknown.
-   Merchant/account routing must support shared and dedicated Faspay
    accounts without allowing clients to select unauthorized accounts.
-   Two applications sharing one merchant account can use the same
    invoice number; provider-side references must not collide.
-   The runtime is serverless (Vercel) on a free-tier-first budget, so
    continuous workers are not available and infrastructure is limited
    to Vercel, Neon, and Upstash QStash.

## 3. Goals

1.  Provide one internal API and one normalized payload for payment
    creation and status retrieval, identical for every consumer
    platform.
2.  Centralize Faspay communication (SNAP and Legacy) and credential
    management.
3.  Support multiple applications and a mixture of shared and dedicated
    merchant accounts.
4.  Reliably route provider callbacks to the correct application.
5.  Prevent duplicate payment creation and duplicate business effects
    through idempotency.
6.  Preserve an auditable record of provider requests, callbacks,
    deliveries, and status transitions.
7.  Support retries and reconciliation for temporary failures and
    ambiguous outcomes without continuous workers.
8.  Migrate existing applications incrementally without losing
    visibility of in-flight transactions.
9.  Stay lightweight: Vercel, Neon PostgreSQL, and Upstash QStash (as
    scheduler only); no Celery, no Redis queue, no dedicated worker
    server.

## 4. Non-Goals

-   Replacing Faspay as the payment provider.
-   Building a customer-facing checkout UI (consumers render the
    returned payment instruction or redirect URL).
-   Acting as a ledger or accounting system of record.
-   Guaranteeing that a payment succeeds; the gateway reports validated
    provider outcomes.
-   Supporting providers other than Faspay in the MVP (Midtrans and
    MutasiBank stay in the legacy application until a later phase),
    although provider-specific code must be isolated.
-   Automatic refunds, reversals, or cancellations unless the relevant
    provider operation is explicitly supported and approved.
-   The payment cancellation API (`POST /v1/payments/{payment_id}/cancel`)
    is post-MVP. The `CANCELLED` status is still recorded when a
    provider reports it.
-   Customer-facing return/redirect pages (the legacy `GET
    faspay-callback` redirect stays with the consumer application).
-   Continuous background workers or frequent Vercel Hobby cron jobs.

## 5. Stakeholders and Users

-   **Consumer application (MVP):** **Fluency Hub** and **Crowdfunding**
    (the `ki-cf` platform, which already serves several tenant sites;
    each tenant site is registered as its own gateway client).
-   **Consumer application (future):** other applications such as RWI,
    ZAMS, and Donol, onboarded later using the same contract.
-   **Platform administrator:** Manages client registrations, merchant
    accounts, routes, and webhook endpoints.
-   **Operations/support:** Investigates failed requests, unmatched
    callbacks, delivery failures, and reconciliation discrepancies.
-   **Engineering team:** Maintains the gateway, provider adapters,
    deployment, and integrations.
-   **Finance/reconciliation team:** Compares gateway transaction state
    with provider records and resolves discrepancies.

## 6. Product Scope

### 6.1 MVP

-   Client registry and **OAuth 2.0 client-credentials**
    server-to-server authentication.
-   Merchant-account registry with environment-variable credential
    references (secrets are never stored in the database).
-   Payment-channel catalogue (method, channel code, protocol) and
    per-client routing rules by payment method and environment.
-   Create-payment API and payment status API with a unified payload.
-   **Faspay SNAP driver** (VA, QRIS, e-wallet) and **Faspay Legacy
    driver** (retail Alfagroup and Indomaret; fallback for other
    methods), kept strictly separate in code, configuration, ingress
    routes, logs, and metrics.
-   Dedicated callback ingress for each protocol, one per deployed
    environment, subject to Faspay's callback registration scope.
-   Durable webhook inbox and database-outbox job processing (claimed
    with `FOR UPDATE SKIP LOCKED`).
-   Delivery attempt tracking and retry with exponential backoff.
-   Payment request idempotency and callback deduplication.
-   Status normalization and guarded state transitions.
-   Reconciliation jobs for unknown, pending, and unmatched
    transactions using protocol-appropriate inquiry.
-   Audit logging, metrics, and operational alerts.
-   Externally scheduled job runs (Upstash QStash).
-   Administration through audited engineering scripts and
    token-protected internal endpoints (clients, merchants, routes,
    channels, replay, reconciliation); no admin UI in the MVP.
-   Staged migration from direct integration.

### 6.2 Payment methods and protocols

| Method | Channels (initial) | Primary protocol | Notes |
|---|---|---|---|
| `VA` | Faspay-enabled bank VAs | SNAP | Create VA and status inquiry; payment notification |
| `QRIS` | LinkAja QRIS `715`, ShopeePay QRIS `711`, CIMB QRIS `842` (and `836`, seen in the existing code) | SNAP | QR generate, query, notify |
| `EWALLET` | DANA `722`, `819`; LinkAja `716` | SNAP | Direct debit host-to-host and status |
| `RETAIL` | Alfagroup `707` (Alfamart; Alfamidi coverage to be confirmed); Indomaret `706` | **Legacy** | Payment code is the legacy `trx_id` |

Channel codes are catalogue data, not hard-coded. The exact set of
active channels per merchant must be confirmed per merchant account. A
channel may exist in the catalogue under both protocols (for example VA
via SNAP and via Legacy); a route selects exactly one.

### 6.3 Future Enhancements

-   Additional payment providers (Midtrans, MutasiBank).
-   Merchant routing by more advanced policies.
-   Payment cancellation API (where the provider supports it).
-   Self-service client onboarding.
-   Admin UI for merchant and delivery management.
-   Automated report ingestion and reconciliation.
-   Redis (for example Upstash) for caching, rate limiting, or
    distributed locking if measurements justify it.
-   Advanced analytics and operational SLA reporting.

## 7. Functional Requirements

  ----------------------------------------------------------------------------
  ID                Requirement       Priority          Acceptance criteria
  ----------------- ----------------- ----------------- ----------------------
  FR-001            Client registry   Must              Every consumer
                                                        application has a
                                                        unique client identity
                                                        and active/inactive
                                                        status.

  FR-002            Client            Must              Calls without a valid
                    authentication                      OAuth access token, or
                                                        from an
                                                        inactive/revoked
                                                        client, cannot reach
                                                        protected APIs within
                                                        30 seconds of client
                                                        deactivation or
                                                        credential
                                                        revocation.

  FR-003            Merchant registry Must              Each Faspay merchant
                                                        configuration has a
                                                        unique code,
                                                        environment, status,
                                                        and an environment
                                                        credential prefix.

  FR-004            Merchant routing  Must              Gateway selects only
                                                        an active route
                                                        authorized for the
                                                        authenticated client,
                                                        payment method, and
                                                        channel.

  FR-005            Create payment    Must              Consumer applications
                                                        can request payment
                                                        creation through one
                                                        normalized API for
                                                        VA, QRIS, EWALLET, and
                                                        RETAIL.

  FR-006            Payment           Must              Every payment stores
                    correlation                         an internal ID, client
                                                        reference, merchant
                                                        scope, gateway-generated
                                                        provider partner
                                                        reference, and the
                                                        provider identifiers
                                                        needed for callback
                                                        correlation.

  FR-007            Idempotent create Must              Same client,
                                                        idempotency key, and
                                                        request fingerprint
                                                        returns the original
                                                        operation result; key
                                                        reuse with different
                                                        input returns
                                                        conflict.

  FR-008            Status inquiry    Must              Client can retrieve
                                                        only its own
                                                        transaction status.

  FR-009            Webhook           Must              SNAP callbacks are
                    verification                        verified with the
                                                        SNAP asymmetric
                                                        signature mechanism;
                                                        Legacy callbacks are
                                                        verified by the
                                                        legacy mechanism
                                                        (inquiry-confirmed)
                                                        before trusted
                                                        processing. SNAP
                                                        success is also
                                                        inquiry-confirmed
                                                        while
                                                        `CALLBACK_VERIFY_MODE`
                                                        is
                                                        `SIGNATURE_AND_INQUIRY`
                                                        (the default until
                                                        the TRD release
                                                        gates close).

  FR-010            Durable webhook   Must              A valid callback is
                    inbox                               persisted before a
                                                        success
                                                        acknowledgement is
                                                        returned to Faspay.

  FR-011            Callback routing  Must              Callback is routed
                                                        using stored
                                                        transaction/provider
                                                        references, not
                                                        amount, timing, or
                                                        caller-supplied
                                                        destination.

  FR-012            Callback          Must              Duplicate provider
                    deduplication                       notifications do not
                                                        cause duplicate
                                                        business effects.

  FR-013            Delivery retry    Must              Transient consumer
                                                        endpoint failures are
                                                        retried with
                                                        exponential backoff
                                                        and jitter according
                                                        to configurable
                                                        policy.

  FR-014            Delivery audit    Must              Every delivery has
                                                        status, attempt count,
                                                        last response/error
                                                        category, and
                                                        timestamps.

  FR-015            Reconciliation    Must              Unknown outcomes,
                                                        stale pending
                                                        transactions, and
                                                        unmatched callbacks
                                                        are discoverable and
                                                        can be reconciled.

  FR-016            Status transition Must              Invalid or stale
                    control                             status transitions
                                                        cannot silently
                                                        overwrite a valid
                                                        final state.

  FR-017            Audit logs        Must              Administrative changes
                                                        and critical payment
                                                        state changes are
                                                        auditable.

  FR-018            Environment       Must              Sandbox and production
                    isolation                           credentials,
                                                        endpoints, data, and
                                                        routes are isolated.

  FR-019            Consumer webhook  Must              Outbound gateway
                    signing                             callbacks are signed
                                                        and include a stable
                                                        event ID.

  FR-020            Operational       Must              Operations can monitor
                    monitoring                          create failures,
                                                        unknown outcomes,
                                                        unmatched callbacks,
                                                        job backlog, and
                                                        delivery failures per
                                                        protocol.

  FR-021            Protocol          Must              SNAP and Legacy code
                    separation                          paths, credentials,
                                                        ingress routes, logs,
                                                        and metrics are
                                                        separate and labelled.
                                                        A route resolves to
                                                        exactly one protocol.

  FR-022            Unified payload   Must              Fluency Hub and
                                                        Crowdfunding send and
                                                        receive the same
                                                        request, response, and
                                                        event shapes, carrying
                                                        the identifiers
                                                        existing schemas need
                                                        (`merchant_reference`,
                                                        `provider_reference`,
                                                        `payment_code`,
                                                        metadata).

  FR-023            Triggered job     Must              Background work runs
                    execution                           only through bounded,
                                                        authenticated trigger
                                                        calls (QStash or
                                                        post-commit QStash nudge),
                                                        with safe concurrent
                                                        claiming.

  FR-024            Channel           Must              Methods and channel
                    catalogue                           codes are data-driven
                                                        and can be enabled or
                                                        disabled per merchant
                                                        and client without a
                                                        deployment.
  ----------------------------------------------------------------------------

## 8. Business Rules

1.  The authenticated client identity (from the OAuth token) is
    authoritative; an application code supplied in a request body must
    not override it.
2.  A client may use only routes explicitly assigned to it.
3.  A transaction is permanently associated with the merchant
    account, environment, channel, and **protocol** used to create it
    (immutable once the provider call starts).
4.  `payment_id` is generated by the gateway and is the primary internal
    payment identifier.
5.  `merchant_reference` is scoped to a client. `(client_id,
    merchant_reference)` must be unique among payments that are not
    `FAILED`, `EXPIRED`, or `CANCELLED` (rule 7). It maps to the
    consumer's existing transaction identifier (for example `id_transaksi` /
    `id_donasi`).
6.  The gateway generates its own **provider partner reference**
    (compact, unique per merchant account) and sends that to Faspay as
    `partnerReferenceNo` / `trxId` / `bill_no`. The consumer's
    `merchant_reference` is **not** sent as the provider key, because
    two clients sharing one merchant account could reuse the same
    invoice number.
7.  After a payment reaches `FAILED`, `EXPIRED`, or `CANCELLED`, the
    same `merchant_reference` may be used for a new payment (new
    `Idempotency-Key`, new `payment_id`, new provider partner
    reference). Only one payment outside those statuses may exist per
    `(client_id, merchant_reference)`, so a `SUCCEEDED` or `UNKNOWN`
    payment blocks a duplicate. Transaction history is never deleted.
8.  A payment must not be marked successful solely because an unverified
    callback claims success.
9.  A callback that cannot be mapped unambiguously is stored as
    `UNMATCHED` for reconciliation; do not guess its destination.
10. Amount, currency, merchant scope, and available provider identifiers
    must be checked before applying a callback.
11. Provider-specific status and raw identifiers are retained separately
    from the normalized internal status.
12. A timeout during payment creation is an unknown outcome until
    checked; it is not automatically a failure.
13. **No automatic cross-protocol failover on create.** If a SNAP create
    is ambiguous, the gateway must not retry the same payment through
    Legacy (or the reverse); that risks two live payments. Switching
    protocol for a route is an administrator action taken only after
    the previous outcome is known.
14. Consumer callback delivery is at-least-once. Consumer applications
    must process events idempotently.
15. Provider credentials live in environment variables (never in the
    database or in logs); the database holds only the environment
    prefix reference.
16. Amounts are accepted as integer IDR rupiah and converted to the
    provider format only inside the adapter; they are stored as integer
    rupiah.
17. `Idempotency-Key` is required on create-payment requests.
18. Each tenant site of a multi-tenant consumer (for example a
    Crowdfunding tenant) is its own gateway client, so
    `merchant_reference`, webhook destination, and secrets are scoped
    per tenant.
19. In the MVP, administration (clients, merchant accounts, routes,
    channels, replay, reconciliation) is performed through audited
    engineering scripts and token-protected internal endpoints; there is
    no admin UI.

## 9. Status Model

Normalized statuses:

-   `CREATED`: internal transaction is committed; the provider has not
    yet been called.
-   `PENDING`: provider accepted the payment and it is awaiting
    completion.
-   `SUCCEEDED`: provider outcome is verified as successful.
-   `FAILED`: verified final failure.
-   `EXPIRED`: verified expiration.
-   `CANCELLED`: valid cancellation outcome.
-   `UNKNOWN`: provider operation outcome cannot yet be established
    (for example a create call that timed out).

Provider status mapping is defined per protocol in the TRD (SNAP
`latestTransactionStatus` / `paymentFlagStatus` codes versus Legacy
`payment_status_code`). A provider "refunded" or "reversal" status is
recorded and alerted but is not an MVP normalized status; it is handled
manually on both protocols and leaves the normalized status unchanged.
The allowed transition table is normative (TRD 11.3) and must be
tested.

## 10. Non-Functional Requirements

  -----------------------------------------------------------------------
  Area                                Requirement
  ----------------------------------- -----------------------------------
  Throughput                          Target **500 requests per second for
                                      all gateway traffic combined**
                                      (token, create, status, callback
                                      ingress, internal job calls). This
                                      is a target with a documented
                                      feasibility risk on free-tier
                                      infrastructure; it must be proven
                                      by load test (see TRD).

  Cost                                Free-tier first (Vercel + Neon). No
                                      always-on workers, no extra paid
                                      infrastructure in the MVP.

  Reliability                         Persist valid callbacks durably
                                      before acknowledging them.

  Consistency                         Payment state and job/delivery
                                      outbox creation occur atomically.

  Security                            TLS, OAuth client authentication,
                                      env-based secrets, callback
                                      verification, least privilege, and
                                      client isolation.

  Idempotency                         Payment creation, callback
                                      processing, and job execution
                                      tolerate retries.

  Observability                       Correlation IDs, structured logs,
                                      metrics, and actionable alerts,
                                      always labelled with the protocol.

  Maintainability                     Provider and protocol behavior is
                                      isolated behind adapters.

  Scalability                         API, ingress, and job-run endpoints
                                      scale independently as serverless
                                      functions.

  Recovery                            Failed jobs can be retried or
                                      replayed with audit controls.

  Data minimization                   Store and forward only required
                                      customer/payment data; redact
                                      sensitive values in logs. Retention
                                      purges keep the small database
                                      within free-tier storage.

  Availability                        Define measurable service-level
                                      objectives before production
                                      rollout.
  -----------------------------------------------------------------------

## 11. API Overview

Client API base path: `/v1`

  ---------------------------------------------------------------------------------------------
  Method              Endpoint                                      Purpose
  ------------------- --------------------------------------------- ---------------------------
  POST                `/v1/oauth/token`                             OAuth client-credentials
                                                                    token

  POST                `/v1/payments`                                Create payment (all methods)

  GET                 `/v1/payments/{payment_id}`                   Retrieve payment status

  GET                 `/v1/payments/by-reference/{merchant_reference}`  Retrieve a client-owned
                                                                    payment by reference

  POST                `/v1.0/transfer-va/payment`                   **SNAP** VA/e-wallet payment
                                                                    notification ingress
                                                                    (provider-authenticated)

  POST                `/v1.0/qr/qr-mpm-notify`                      **SNAP** QRIS payment
                                                                    notification ingress
                                                                    (provider-authenticated)

  POST                `/v1.0/debit/notify`                          **SNAP** e-wallet payment
                                                                    notification ingress
                                                                    (provider-authenticated)

  POST                `/v1/webhooks/faspay/legacy/notification`     **Legacy** payment
                                                                    notification ingress
                                                                    (provider-authenticated)

  POST                `/internal/jobs/run`                          Bounded job run (QStash /
                                                                    trigger only)

  POST                `/internal/reconciliation/{payment_id}`       Admin-token reconciliation
                                                                    trigger

  POST                `/internal/deliveries/{delivery_id}/replay`   Admin-token replay of a
                                                                    dead-lettered delivery

  POST                `/internal/webhook-events/{event_id}/reprocess` Admin-token reprocess of
                                                                    an unmatched inbox event
  ---------------------------------------------------------------------------------------------

SNAP ingress paths follow the SNAP standard because Faspay appends them
to the registered merchant URL. Detailed schemas and error behavior are
defined in the TRD.

## 12. Migration Requirements

1.  Inventory current application integrations (Fluency Hub,
    Crowdfunding), merchant accounts, payment methods, channel codes,
    protocols in use, callback URLs, provider identifiers, and
    in-flight transactions.
2.  Build the gateway and validate it in sandbox for both protocols.
3.  Pilot one low-risk application.
4.  Cut over create-payment traffic and callback configuration in a
    controlled window per merchant account.
5.  Keep old callback handling available for in-flight transactions
    where permitted.
6.  Reconcile gateway, application, and Faspay records after pilot.
7.  Migrate remaining applications one at a time.
8.  Remove direct integrations only after successful validation and a
    documented rollback window.

Do not dual-submit real payment-creation requests to old and new
integrations. Existing legacy identifiers (`id_transaksi` / `id_donasi`
as `merchant_reference`, existing `trx_id` values) must remain
resolvable through the gateway for in-flight transactions.

## 13. Risks and Mitigations

  -----------------------------------------------------------------------
  Risk                                Mitigation
  ----------------------------------- -----------------------------------
  SNAP callback headers, retry        Treat as release gates; verify with
  policy, and notification paths are  Faspay sandbox payloads; keep an
  not fully documented                inquiry confirmation before
                                      marking success.

  Callback cannot be correlated       Confirm provider identifiers before
                                      build; retain unmatched inbox and
                                      reconciliation workflow.

  Duplicate payment after timeout     Mark outcome unknown; query
                                      provider or use documented provider
                                      idempotency before retry; no
                                      cross-protocol failover.

  Incorrect merchant routing          Server-side route allowlist and
                                      environment/account validation.

  Shared merchant reference collision Gateway-generated provider partner
                                      reference.

  Consumer application is down        Durable outbox, retry, dead-letter,
                                      and replay controls.

  Provider callback is forged         Implement the documented SNAP
                                      signature verification; confirm
                                      legacy callbacks by inquiry.

  Old transactions lose callbacks     Per-merchant cutover plan and
  during cutover                      reconciliation of in-flight
                                      transactions.

  500 rps exceeds free-tier limits    Load test early; keep the hot path
                                      to few DB round trips; document
                                      scaling path (paid Neon/Vercel
                                      tiers, caching).

  Serverless limits (duration, cold   Bounded job batches, lightweight
  starts, connections)                dependencies, pooled Neon
                                      connection string.

  Non-static Vercel egress IPs        Confirm whether Faspay requires
  conflict with provider IP           outbound IP allow-listing; if so
  allow-listing                       plan static egress.

  Scheduler outage                    Post-commit nudge plus reconciliation
                                      on the next run; alert on oldest
                                      pending job age.

  Free-tier database storage          Retention purge job and minimal
                                      payload storage.
  -----------------------------------------------------------------------

## 14. Decisions Log (resolved during brainstorming)

| Topic | Decision |
|---|---|
| Stack | FastAPI, async SQLAlchemy 2.0 + asyncpg, Python 3.12, modelled on the `ki-cf-v5` layout |
| Hosting / DB | Vercel serverless + Neon PostgreSQL (single database) |
| Consumer authentication | OAuth 2.0 client credentials |
| Background work | Database outbox, `FOR UPDATE SKIP LOCKED`, exponential backoff, idempotent jobs; no Celery / Redis queue / worker server |
| Scheduling | External scheduler (**Upstash QStash**); no frequent Vercel Hobby cron |
| Data access | ORM models + Alembic for migrations; **raw SQL** (`text()`) for runtime functionality |
| Secrets | Environment variables |
| Providers | Faspay only (MVP) |
| Protocols | SNAP and Legacy both implemented, clearly separated |
| Methods | VA, QRIS, EWALLET (SNAP); RETAIL Alfagroup + Indomaret (Legacy) |
| Consumers | Fluency Hub and Crowdfunding, one shared payload contract |
| Throughput target | 500 rps, all traffic combined |
| Amount unit | Integer IDR rupiah in the API and the database; the adapter converts to the provider format |
| Cancellation | Post-MVP; no cancel endpoint in the MVP |
| Retry after final failure | Allowed: new payment for the same `merchant_reference` after `FAILED`, `EXPIRED`, or `CANCELLED`; one live payment (not `FAILED`, `EXPIRED`, or `CANCELLED`) per reference |
| Crowdfunding tenants | Each tenant site is its own gateway client |
| Administration | Engineering scripts plus `ADMIN_API_TOKEN` internal endpoints; no admin UI in the MVP |
| VA number | Assigned by Faspay (confirmed); the create request carries none |
| Merchant ID | The same value is used for SNAP and Legacy (confirmed) |
| SNAP notifications | Three routes (VA, QRIS, e-wallet) served by one handler; payload parsed by shape; replies per TRD 9.2 |
| Document version | Stays v1.0 while brainstorming |

## 15. Open Questions

Questions that depend on Faspay (callback scope and headers, retry
policy, e-wallet notification path, status-code mapping, legacy reply
and signature, channel codes, Alfamidi coverage, IP allow-listing) are
tracked in one place: TRD section 20. Product and business questions
still open:

-   What are the required availability target, event-retention period,
    and delivery SLA? (Proposed defaults are in TRD 12.7 and ERD
    section 6.)
-   What are the default and maximum `expires_at` per channel? (The
    existing integrations use 5 hours for SNAP and 1 day for Legacy.)
-   Which payment methods and inquiry operations are enabled for each
    merchant?

## 16. Definition of Done

-   Functional requirements have automated tests.
-   Sandbox integration tests pass for each relevant merchant
    configuration, separately for SNAP and Legacy.
-   Duplicate, delayed, malformed, and unmatched callback scenarios are
    tested for both protocols.
-   Timeout/unknown outcomes and reconciliation are tested.
-   Concurrent job claiming (`SKIP LOCKED`) and lease expiry are tested.
-   A load test demonstrates the throughput achieved against the 500 rps
    target, with the result and limits documented.
-   Security review covers client isolation, callback verification,
    secret handling, and destination URL controls.
-   Monitoring and operational runbooks are available.
-   Pilot and rollback procedures are approved.
-   Finance/operations validate transaction reconciliation before
    general rollout.
