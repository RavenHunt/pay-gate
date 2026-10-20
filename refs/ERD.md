# Entity Relationship Design (ERD)

## Faspay Payment Gateway Orchestrator (SNAP + Legacy)

-   **Document status:** Draft for review (brainstorming)
-   **Version:** 1.0
-   **Date:** 12 October 2026 (last revised 20 October 2026)
-   **Database:** Neon PostgreSQL, single database for all clients
-   **Related documents:** `PRD-Faspay-Payment-Gateway-Orchestrator.md`,
    `TRD-Faspay-Payment-Gateway-Orchestrator.md`

## 1. Design Principles

-   One database for all clients; isolation is enforced by `client_id`
    on every client-owned row and by constraints, not by separate
    databases.
-   Separate application identity from Faspay merchant identity.
-   Persist the merchant account, environment, channel, and **protocol**
    (`SNAP` or `LEGACY`) selected for each transaction.
-   Keep provider-specific references alongside an internal payment ID;
    never use the consumer's invoice number as the provider key.
-   Make the webhook inbox, the job outbox, and the delivery records
    durable. The `gateway_jobs` table is the single claim queue
    (`FOR UPDATE SKIP LOCKED`).
-   Use database constraints to enforce idempotency and ownership
    boundaries.
-   Store environment-variable prefixes (references), not provider
    secrets or private keys.
-   Preserve audit history rather than overwriting important provider
    events.
-   Keep the free-tier database small: store redacted payloads only and
    purge by retention policy.
-   ORM models exist for schema definition and Alembic migrations;
    runtime queries are raw SQL, so the schema stays portable.

## 2. Entity Relationship Diagram

``` mermaid
erDiagram
    PAYMENT_CLIENTS ||--o{ CLIENT_CREDENTIALS : authenticates
    PAYMENT_CLIENTS ||--o{ CLIENT_MERCHANT_ROUTES : has
    MERCHANT_ACCOUNTS ||--o{ CLIENT_MERCHANT_ROUTES : assigned
    PAYMENT_CHANNELS ||--o{ CLIENT_MERCHANT_ROUTES : used_by
    PAYMENT_CLIENTS ||--o{ PAYMENT_TRANSACTIONS : owns
    MERCHANT_ACCOUNTS ||--o{ PAYMENT_TRANSACTIONS : processes
    PAYMENT_CHANNELS ||--o{ PAYMENT_TRANSACTIONS : via
    PAYMENT_TRANSACTIONS ||--o{ PAYMENT_ATTEMPTS : has
    PAYMENT_TRANSACTIONS ||--o{ WEBHOOK_EVENTS : correlates
    MERCHANT_ACCOUNTS ||--o{ WEBHOOK_EVENTS : receives
    PAYMENT_TRANSACTIONS ||--o{ PAYMENT_EVENTS : emits
    WEBHOOK_EVENTS |o--o{ PAYMENT_EVENTS : triggers
    PAYMENT_EVENTS ||--o{ WEBHOOK_DELIVERIES : dispatches
    PAYMENT_CLIENTS ||--o{ WEBHOOK_DELIVERIES : receives
    %% GATEWAY_JOBS.ref_id is polymorphic (read per job_type); it has no foreign key
    PAYMENT_CLIENTS ||--o{ API_IDEMPOTENCY_KEYS : owns
    PAYMENT_TRANSACTIONS |o--o{ AUDIT_LOGS : references

    PAYMENT_CLIENTS {
        uuid id PK
        varchar app_code UK
        varchar name
        varchar status
        text webhook_url
        varchar webhook_secret_ref
        timestamp created_at
        timestamp updated_at
    }

    CLIENT_CREDENTIALS {
        uuid id PK
        uuid client_id FK
        varchar oauth_client_id UK
        varchar secret_hash
        text_array scopes
        varchar status
        timestamp expires_at
        timestamp last_used_at
        timestamp revoked_at
        timestamp created_at
    }

    MERCHANT_ACCOUNTS {
        uuid id PK
        varchar merchant_code UK
        varchar provider
        varchar environment
        varchar credential_ref
        varchar partner_id
        varchar provider_merchant_id
        varchar merchant_name
        varchar status
        varchar callback_config_ref
        timestamp created_at
        timestamp updated_at
    }

    PAYMENT_CHANNELS {
        uuid id PK
        varchar payment_method
        varchar channel_code
        varchar protocol
        varchar display_name
        varchar provider_category
        varchar status
        timestamp created_at
        timestamp updated_at
    }

    CLIENT_MERCHANT_ROUTES {
        uuid id PK
        uuid client_id FK
        uuid merchant_account_id FK
        uuid payment_channel_id FK
        varchar payment_method
        varchar channel_code
        varchar route_code
        int priority
        boolean is_default
        varchar status
        timestamp created_at
        timestamp updated_at
    }

    PAYMENT_TRANSACTIONS {
        uuid id PK
        uuid client_id FK
        uuid merchant_account_id FK
        uuid payment_channel_id FK
        varchar protocol
        varchar channel_code
        varchar merchant_reference
        varchar provider_partner_reference
        varchar provider_reference
        bigint amount
        char currency
        varchar payment_method
        varchar status
        varchar provider_status
        jsonb instruction
        jsonb metadata
        timestamp expires_at
        timestamp paid_at
        timestamp created_at
        timestamp updated_at
    }

    PAYMENT_ATTEMPTS {
        uuid id PK
        uuid payment_id FK
        int attempt_no
        varchar operation
        varchar protocol
        varchar external_id
        varchar outcome
        int http_status
        varchar provider_response_code
        varchar provider_reference
        varchar request_fingerprint
        varchar error_category
        timestamp started_at
        timestamp finished_at
    }

    PAYMENT_EVENTS {
        uuid id PK
        uuid payment_id FK
        uuid webhook_event_id FK
        varchar event_type
        varchar status_from
        varchar status_to
        varchar source
        timestamp occurred_at
        timestamp created_at
    }

    WEBHOOK_EVENTS {
        uuid id PK
        uuid merchant_account_id FK
        uuid payment_id FK
        varchar protocol
        varchar source_path
        varchar provider_event_key
        varchar event_fingerprint UK
        varchar event_type
        varchar provider_status_code
        varchar processing_status
        boolean signature_valid
        varchar verification_method
        jsonb payload_redacted
        timestamp received_at
        timestamp processed_at
    }

    WEBHOOK_DELIVERIES {
        uuid id PK
        uuid payment_event_id FK
        uuid client_id FK
        text destination_url
        varchar delivery_status
        int attempt_count
        int last_http_status
        varchar last_error_category
        timestamp next_retry_at
        timestamp delivered_at
        timestamp created_at
    }

    GATEWAY_JOBS {
        uuid id PK
        varchar job_type
        varchar status
        varchar dedupe_key UK
        uuid ref_id
        jsonb args
        int attempts
        int max_attempts
        timestamp run_after
        timestamp locked_until
        varchar locked_by
        varchar last_error_category
        timestamp created_at
        timestamp updated_at
        timestamp finished_at
    }

    API_IDEMPOTENCY_KEYS {
        uuid id PK
        uuid client_id FK
        varchar idempotency_key
        varchar request_fingerprint
        varchar operation_status
        uuid payment_id FK
        jsonb response_snapshot
        timestamp created_at
        timestamp expires_at
    }

    AUDIT_LOGS {
        uuid id PK
        uuid payment_id FK
        varchar entity_type
        varchar entity_id
        varchar actor_type
        varchar actor_id
        varchar action
        varchar correlation_id
        jsonb metadata_redacted
        timestamp created_at
    }
```

## 3. Entity Definitions

### 3.1 `payment_clients`

Represents an application allowed to use the gateway (MVP: Fluency Hub
and Crowdfunding). Each Crowdfunding tenant site is its own client.

  -----------------------------------------------------------------------
  Column                  Type (suggested)        Constraints / notes
  ----------------------- ----------------------- -----------------------
  `id`                    UUID                    Primary key

  `app_code`              VARCHAR(64)             Unique, immutable
                                                  application code

  `name`                  VARCHAR(160)            Display name

  `status`                VARCHAR(20)             `ACTIVE`, `INACTIVE`

  `webhook_url`           TEXT                    Registered HTTPS
                                                  destination; protect
                                                  against SSRF

  `webhook_secret_ref`    VARCHAR(255)            Environment-variable
                                                  full name of the
                                                  outbound signature
                                                  secret

  `created_at`,           TIMESTAMPTZ             Audit timestamps
  `updated_at`                                    
  -----------------------------------------------------------------------

### 3.2 `client_credentials`

OAuth 2.0 client-credentials identities. A client can hold several
credentials so secrets can be rotated without downtime.

  -----------------------------------------------------------------------
  Column                  Type (suggested)        Constraints / notes
  ----------------------- ----------------------- -----------------------
  `id`                    UUID                    Primary key

  `client_id`             UUID                    FK to `payment_clients`

  `oauth_client_id`       VARCHAR(64)             Unique public
                                                  identifier presented
                                                  at the token endpoint

  `secret_hash`           VARCHAR(255)            Hash of the client
                                                  secret (Argon2id, or
                                                  keyed HMAC-SHA-256
                                                  with a server pepper
                                                  for high-entropy
                                                  generated secrets).
                                                  Never the plaintext.

  `scopes`                TEXT[]                  Allowed scopes, for
                                                  example
                                                  `payments:write`,
                                                  `payments:read`

  `status`                VARCHAR(20)             `ACTIVE`, `REVOKED`

  `expires_at`,           TIMESTAMPTZ, nullable   Lifecycle timestamps
  `last_used_at`,                                 
  `revoked_at`                                    

  `created_at`            TIMESTAMPTZ             Audit timestamp
  -----------------------------------------------------------------------

The access token is a short-lived signed JWT issued by the gateway
(`sub` = `client_id`, `cred` = credential `id`); no token table is
required. See the TRD for validation and revocation.

### 3.3 `merchant_accounts`

Represents one Faspay merchant configuration in one environment.
Non-secret identifiers are columns; secrets are environment variables
resolved from `credential_ref`.

  -----------------------------------------------------------------------
  Column                  Type (suggested)        Constraints / notes
  ----------------------- ----------------------- -----------------------
  `id`                    UUID                    Primary key

  `merchant_code`         VARCHAR(64)             Unique internal
                                                  identifier

  `provider`              VARCHAR(32)             Initially `FASPAY`

  `environment`           VARCHAR(16)             `SANDBOX`, `PRODUCTION`

  `credential_ref`        VARCHAR(64)             Environment-variable
                                                  **prefix**, for example
                                                  `KRN`, resolving
                                                  `KRN_PRIVATE_KEY`
                                                  (existing convention)
                                                  and the other variables
                                                  listed in the TRD. Not
                                                  a secret.

  `partner_id`            VARCHAR(16), nullable   SNAP `X-PARTNER-ID`
                                                  (confirm the length
                                                  with Faspay)

  `provider_merchant_id`  VARCHAR(64), nullable   Faspay merchant ID used
                                                  in payloads and for
                                                  matching inbound
                                                  callbacks (`merchantId`
                                                  / `merchant_id`)

  `merchant_name`         VARCHAR(160), nullable  Legacy `merchant`
                                                  field

  `status`                VARCHAR(20)             `ACTIVE`, `INACTIVE`,
                                                  `MAINTENANCE`

  `callback_config_ref`   VARCHAR(255), nullable  Optional note on the
                                                  provider callback
                                                  registration

  `created_at`,           TIMESTAMPTZ             Audit timestamps
  `updated_at`                                    
  -----------------------------------------------------------------------

Suggested uniqueness: `(environment, provider_merchant_id)` where not
null, so an inbound callback can resolve exactly one merchant account.
Do not assume merchant ID, terminal ID, or callback settings are
globally unique unless Faspay confirms that scope. The merchant ID is
the same value for SNAP (`merchantId`) and Legacy (`merchant_id`)
(confirmed), so one column serves both protocols.

### 3.4 `payment_channels`

Catalogue of methods and channel codes, with the protocol that serves
each. This makes methods data-driven (FR-024).

  -----------------------------------------------------------------------
  Column                  Type (suggested)        Constraints / notes
  ----------------------- ----------------------- -----------------------
  `id`                    UUID                    Primary key

  `payment_method`        VARCHAR(16)             `VA`, `QRIS`,
                                                  `EWALLET`, `RETAIL`

  `channel_code`          VARCHAR(32)             Faspay channel code
                                                  (SNAP `channelCode` or
                                                  legacy `payment_channel`)

  `protocol`              VARCHAR(8)              `SNAP` or `LEGACY`

  `display_name`          VARCHAR(80)             For example `Alfagroup`

  `provider_category`     VARCHAR(32), nullable   Adapter hint (for
                                                  example `va_bank`,
                                                  `qris`, `e-wallet`)

  `status`                VARCHAR(20)             `ACTIVE`, `INACTIVE`

  `created_at`,           TIMESTAMPTZ             Audit timestamps
  `updated_at`                                    
  -----------------------------------------------------------------------

Suggested uniqueness: `(payment_method, channel_code, protocol)`.

Initial seed (to be confirmed per merchant). One wallet can carry two
codes (for example LinkAja `716` / `302`, DANA `819` / `722`); list each
code as its own row:

  -----------------------------------------------------------------------
  Method        Channel code                   Protocol
  ------------- ------------------------------ ---------------------------
  `QRIS`        `715`, `711`, `842`, `836`     `SNAP`

  `EWALLET`     `722`, `819` (DANA), `716`     `SNAP`
                (LinkAja)                      

  `VA`          Faspay bank VA channel codes   `SNAP`
                (per merchant)                 

  `RETAIL`      `707` (Alfagroup: Alfamart;    `LEGACY`
                Alfamidi unconfirmed), `706` (Indomaret)   
  -----------------------------------------------------------------------

### 3.5 `client_merchant_routes`

Defines which merchant account and channel a client may use. Because a
channel row fixes the protocol, a route resolves to exactly one
protocol.

  -----------------------------------------------------------------------
  Column                  Type (suggested)        Constraints / notes
  ----------------------- ----------------------- -----------------------
  `id`                    UUID                    Primary key

  `client_id`             UUID                    FK to `payment_clients`

  `merchant_account_id`   UUID                    FK to
                                                  `merchant_accounts`

  `payment_channel_id`    UUID                    FK to `payment_channels`

  `payment_method`        VARCHAR(16)             Copy of the channel's
                                                  method (immutable;
                                                  used by the partial
                                                  unique index)

  `channel_code`          VARCHAR(32)             Copy of the channel's
                                                  code (immutable; used
                                                  by the partial unique
                                                  index)

  `route_code`            VARCHAR(64)             Client-visible alias,
                                                  e.g. `DEFAULT`

  `priority`              INTEGER                 Informational
                                                  ordering for admin
                                                  tooling; not used to
                                                  resolve a request

  `is_default`            BOOLEAN                 Default for the
                                                  applicable
                                                  client/method; used
                                                  when the request
                                                  omits `route_code`

  `status`                VARCHAR(20)             `ACTIVE`, `INACTIVE`

  `created_at`,           TIMESTAMPTZ             Audit timestamps
  `updated_at`                                    
  -----------------------------------------------------------------------

Uniqueness: `(client_id, route_code, payment_channel_id,
merchant_account_id)`, plus two partial unique indexes: one on
`(client_id, route_code, payment_method, channel_code)` where
`status = 'ACTIVE'`, so a request resolves to at most one route and one
protocol, and one on `(client_id, payment_method)` where
`is_default AND status = 'ACTIVE'`, so there is at most one default.
Routes are never chosen by a client-supplied merchant ID. To switch a
channel between protocols, an administrator activates a route to the
other protocol's channel row and deactivates the old one; the gateway
never fails over automatically on create.

### 3.6 `payment_transactions`

Canonical internal record for a payment, identical for every method and
protocol.

  ---------------------------------------------------------------------------
  Column                      Type (suggested)        Constraints / notes
  --------------------------- ----------------------- -----------------------
  `id`                        UUID                    Primary key and public
                                                      `payment_id` (time
                                                      ordered, for example
                                                      UUIDv7)

  `client_id`                 UUID                    FK; owner of the
                                                      payment

  `merchant_account_id`       UUID                    FK; immutable after
                                                      the provider call starts

  `payment_channel_id`        UUID                    FK; immutable after
                                                      the provider call starts

  `protocol`                  VARCHAR(8)              `SNAP` or `LEGACY`,
                                                      copied from the channel
                                                      at creation; immutable
                                                      once the provider call
                                                      starts

  `channel_code`              VARCHAR(32)             Copy of the channel
                                                      code at creation

  `merchant_reference`        VARCHAR(128)            Reference supplied by
                                                      the consumer (maps to
                                                      existing identifiers
                                                      such as `id_transaksi`
                                                      / `id_donasi`)

  `provider_partner_reference` VARCHAR(32)            Gateway-generated
                                                      compact key sent to
                                                      Faspay as
                                                      `partnerReferenceNo` /
                                                      `trxId` / `bill_no`;
                                                      keep it at or under the
                                                      strictest documented
                                                      provider length

  `provider_reference`        VARCHAR(128), nullable  Reference assigned by
                                                      Faspay (SNAP
                                                      `referenceNo`, legacy
                                                      `trx_id`; for `VA`, the
                                                      Faspay-assigned VA
                                                      number); used for
                                                      callback correlation

  `amount`                    BIGINT                  Integer rupiah (no
                                                      minor units); the
                                                      adapter converts to
                                                      the provider format
                                                      ("150000.00", or x100
                                                      for Legacy
                                                      `bill_total`)

  `currency`                  CHAR(3)                 `IDR`

  `payment_method`            VARCHAR(16)             `VA`, `QRIS`,
                                                      `EWALLET`, `RETAIL`

  `status`                    VARCHAR(24)             Normalized internal
                                                      status

  `provider_status`           VARCHAR(64), nullable   Latest raw provider
                                                      status code

  `instruction`               JSONB, nullable         Non-secret payment
                                                      instruction returned
                                                      to the client (VA
                                                      number, QR URL,
                                                      redirect URL, payment
                                                      code, expiry)

  `metadata`                  JSONB, nullable         Client-supplied string
                                                      map echoed back in
                                                      responses and events;
                                                      size-limited; carries
                                                      the extra identifiers a
                                                      platform needs

  `expires_at`, `paid_at`     TIMESTAMPTZ, nullable  Business timestamps

  `created_at`, `updated_at`  TIMESTAMPTZ             Audit timestamps
  ---------------------------------------------------------------------------

Suggested constraints:

-   Partial unique index on `(client_id, merchant_reference)` where
    `status NOT IN ('FAILED', 'EXPIRED', 'CANCELLED')`: one live payment
    per consumer reference.
-   `UNIQUE(merchant_account_id, provider_partner_reference)`.
-   Partial unique index on `(merchant_account_id, protocol,
    provider_reference)` where `provider_reference IS NOT NULL`;
    confirm Faspay's uniqueness scope first.
-   `amount` must be positive and within configured limits.
-   `currency`, `payment_method`, `protocol`, and `status` are
    constrained to their allowed value sets.

Customer name, email, and phone are used only to build the provider
request and are not stored (data minimization).

A new payment for the same invoice after `FAILED`, `EXPIRED`, or
`CANCELLED` is a new row (new `id`, new provider partner reference); the
earlier row is kept as history.

### 3.7 `payment_attempts`

Tracks each outbound provider operation (create, inquiry, cancel).

  -----------------------------------------------------------------------
  Column                  Type (suggested)        Notes
  ----------------------- ----------------------- -----------------------
  `id`                    UUID                    Primary key

  `payment_id`            UUID                    FK to transaction

  `attempt_no`            INTEGER                 Sequence within the
                                                  payment/operation

  `operation`             VARCHAR(32)             `CREATE`, `INQUIRY`,
                                                  `CANCEL`

  `protocol`              VARCHAR(8)              `SNAP` or `LEGACY`

  `external_id`           VARCHAR(36), nullable   SNAP `X-EXTERNAL-ID`
                                                  (unique per day)

  `outcome`               VARCHAR(24)             `SUCCEEDED`, `FAILED`,
                                                  `UNKNOWN`, `PENDING`

  `http_status`           INTEGER, nullable       Provider HTTP status

  `provider_response_code` VARCHAR(16), nullable  SNAP 7-character
                                                  response code or
                                                  legacy `response_code`

  `provider_reference`    VARCHAR(128), nullable  Reference observed for
                                                  this attempt

  `request_fingerprint`   VARCHAR(128), nullable  Safe fingerprint; do
                                                  not store credentials

  `error_category`        VARCHAR(64), nullable   Normalized error class

  `started_at`,           TIMESTAMPTZ, nullable   Timing
  `finished_at`                                   
  -----------------------------------------------------------------------

Suggested uniqueness: `(payment_id, operation, attempt_no)`. SNAP
requires `X-EXTERNAL-ID` to be unique within the same day; generate it
as a random UUID so no cross-row check is needed.

A timeout after a create request is recorded as `UNKNOWN`, not
`FAILED`, until reconciled.

### 3.8 `webhook_events`

Durable inbox for provider callbacks, for both protocols.

  -----------------------------------------------------------------------
  Column                  Type (suggested)        Notes
  ----------------------- ----------------------- -----------------------
  `id`                    UUID                    Internal event ID

  `merchant_account_id`   UUID, nullable          Resolved from the
                                                  callback's merchant
                                                  identifier; null if
                                                  not resolvable

  `payment_id`            UUID, nullable          Null for unmatched
                                                  events

  `protocol`              VARCHAR(8)              `SNAP` or `LEGACY`

  `source_path`           VARCHAR(128)            Ingress path that
                                                  received it (for
                                                  example
                                                  `/v1.0/qr/qr-mpm-notify`)

  `provider_event_key`    VARCHAR(255), nullable  Stable provider event
                                                  identifier if
                                                  available

  `event_fingerprint`     VARCHAR(128)            Deterministic
                                                  deduplication
                                                  fingerprint (unique);
                                                  `REJECTED` events add
                                                  a random salt

  `event_type`            VARCHAR(64)             Normalized event type

  `provider_status_code`  VARCHAR(8), nullable    SNAP
                                                  `latestTransactionStatus`
                                                  or legacy
                                                  `payment_status_code`

  `processing_status`     VARCHAR(24)             `PROCESSED`,
                                                  `UNMATCHED` (verified
                                                  but not correlated),
                                                  `REJECTED` (failed
                                                  verification),
                                                  `FAILED`

  `signature_valid`       BOOLEAN                 Verification outcome (Legacy: the inquiry confirmed the bill)

  `verification_method`   VARCHAR(32)             `SNAP_SIGNATURE`,
                                                  `SNAP_INQUIRY_ONLY`,
                                                  `LEGACY_INQUIRY`,
                                                  `SNAP_SIGNATURE_AND_INQUIRY`

  `payload_redacted`      JSONB                   Redacted payload;
                                                  retention-controlled

  `received_at`,          TIMESTAMPTZ, nullable   Processing timestamps
  `processed_at`                                  
  -----------------------------------------------------------------------

Fingerprints are computed over stable attributes only, per protocol:

-   SNAP VA: protocol, merchant account, `virtualAccountNo`,
    `paymentRequestId` / `trxId`, `latestTransactionStatus`,
    `paidAmount.value`.
-   SNAP e-wallet (documented route `/v1.0/debit/notify`; may also
    arrive on the VA path): protocol, merchant account,
    `originalPartnerReferenceNo`, `originalReferenceNo`,
    `latestTransactionStatus`, `amount.value` (when present).
-   SNAP QRIS: protocol, merchant account, `originalReferenceNo`,
    `originalPartnerReferenceNo`, `latestTransactionStatus`,
    `amount.value`.
-   Legacy: protocol, merchant account, `bill_no`, `trx_id`, reported
    status code.

Do not rely solely on hashing the full raw body if fields such as
timestamps change on retries. Duplicate handling (TRD 10.3): a
duplicate of a `PROCESSED` event is acknowledged without effects, and a
duplicate of an `UNMATCHED` event is re-correlated.

### 3.9 `webhook_deliveries`

Tracks each consumer delivery. Claiming and leasing is done through
`gateway_jobs`; this table records delivery state for audit and
support.

  -----------------------------------------------------------------------
  Column                  Type (suggested)        Notes
  ----------------------- ----------------------- -----------------------
  `id`                    UUID                    Primary key

  `payment_event_id`      UUID                    FK to `payment_events`

  `client_id`             UUID                    Consumer application

  `destination_url`       TEXT                    Snapshot of approved
                                                  destination at dispatch
                                                  time

  `delivery_status`       VARCHAR(24)             `PENDING`,
                                                  `RETRY_SCHEDULED`,
                                                  `DELIVERED`,
                                                  `DEAD_LETTER`

  `attempt_count`         INTEGER                 Number of attempts

  `last_http_status`      INTEGER, nullable       Last response

  `last_error_category`   VARCHAR(64), nullable   Normalized error

  `next_retry_at`,        TIMESTAMPTZ, nullable   Retry and completion
  `delivered_at`                                  timestamps

  `created_at`            TIMESTAMPTZ             Audit timestamp
  -----------------------------------------------------------------------

Suggested uniqueness: `(payment_event_id, client_id)`.

`next_retry_at` is a read-only copy of the job's `run_after` for support
views; `gateway_jobs` is authoritative for retry timing.

### 3.10 `gateway_jobs`

The database outbox and single claim queue. Every background activity is
a row here; nothing depends on an always-on worker.

  -----------------------------------------------------------------------
  Column                  Type (suggested)        Notes
  ----------------------- ----------------------- -----------------------
  `id`                    UUID                    Primary key

  `job_type`              VARCHAR(32)             `DELIVER_WEBHOOK`,
                                                  `RETRY_CORRELATION`,
                                                  `INQUIRE_PAYMENT`,
                                                  `RECONCILE_PAYMENT`,
                                                  `EXPIRE_PAYMENT`,
                                                  `PURGE_RETENTION`

  `status`                VARCHAR(16)             `PENDING`, `RUNNING`,
                                                  `RETRY`, `DONE`,
                                                  `DEAD`

  `dedupe_key`            VARCHAR(128), nullable  Unique when set, so
                                                  repeated enqueues are
                                                  idempotent (for example
                                                  `deliver:{event}:{client}`)

  `ref_id`                UUID, nullable          Polymorphic id (no
                                                  FK), read per
                                                  `job_type`: delivery,
                                                  event, or payment

  `args`                  JSONB, nullable         Small non-sensitive
                                                  parameters

  `attempts`,             INTEGER                 Retry accounting
  `max_attempts`                                  

  `run_after`             TIMESTAMPTZ             Earliest execution
                                                  time (backoff target)

  `locked_until`          TIMESTAMPTZ, nullable   Lease expiry; a
                                                  `RUNNING` job past this
                                                  time is reclaimable

  `locked_by`             VARCHAR(64), nullable   Run identifier

  `last_error_category`   VARCHAR(64), nullable   Normalized error

  `created_at`,           TIMESTAMPTZ             Audit timestamps
  `updated_at`,                                   
  `finished_at`                                   
  -----------------------------------------------------------------------

Claim pattern (see the TRD for the exact SQL): select due rows
(`status IN ('PENDING','RETRY') AND run_after <= now()`, or `RUNNING`
with an expired lease) with `FOR UPDATE SKIP LOCKED` and a `LIMIT`, then
mark them `RUNNING` with a lease. Job handlers must be idempotent
because a lease can expire mid-run.

### 3.11 `api_idempotency_keys`

Stores the result of an idempotent API operation.

  -----------------------------------------------------------------------
  Column                  Type (suggested)        Notes
  ----------------------- ----------------------- -----------------------
  `id`                    UUID                    Primary key

  `client_id`             UUID                    FK to client

  `idempotency_key`       VARCHAR(128)            Unique within client

  `request_fingerprint`   VARCHAR(128)            Hash of canonical
                                                  business request

  `operation_status`      VARCHAR(24)             `IN_PROGRESS`,
                                                  `COMPLETED`, `UNKNOWN`,
                                                  `FAILED`

  `payment_id`            UUID, nullable          Resulting payment

  `response_snapshot`     JSONB, nullable         Safe response snapshot,
                                                  excluding secrets

  `created_at`,           TIMESTAMPTZ             Retention and expiry
  `expires_at`                                    
  -----------------------------------------------------------------------

Suggested uniqueness: `(client_id, idempotency_key)`. Do not expire a
key while its operation outcome remains unresolved.

### 3.12 `audit_logs`

Captures administrative and critical operational changes, including
changes to routes, merchant accounts, clients, credentials, and
channels, not only payments.

-   `entity_type` (`ROUTE`, `MERCHANT_ACCOUNT`, `CLIENT`, `CREDENTIAL`,
    `CHANNEL`, `PAYMENT`, `DELIVERY`, `WEBHOOK_EVENT`) and `entity_id`
    (string) identify what changed.
-   `payment_id` is an optional foreign key, set for payment-related
    entries.
-   `actor_type` (`CLIENT`, `ADMIN`, `SYSTEM`) and `actor_id` (string:
    client id, admin operator name from the admin script, or job type).
-   `action`, `correlation_id`, redacted `metadata_redacted`, and
    `created_at`.

Restrict access and define retention. Avoid copying full
payment/customer payloads into audit logs.

### 3.13 `payment_events`

Outbound business events: one row per applied status transition,
whatever caused it (provider callback, inquiry, reconciliation, expiry,
or creation). This is the source of the consumer `event_id`.

  -----------------------------------------------------------------------
  Column                  Type (suggested)        Notes
  ----------------------- ----------------------- -----------------------
  `id`                    UUID                    Primary key; sent to
                                                  consumers as `evt_...`

  `payment_id`            UUID                    FK to transaction

  `webhook_event_id`      UUID, nullable          FK to `webhook_events`
                                                  when a provider
                                                  callback caused it

  `event_type`            VARCHAR(32)             `payment.created`,
                                                  `payment.pending`,
                                                  `payment.succeeded`,
                                                  `payment.failed`,
                                                  `payment.expired`,
                                                  `payment.cancelled`,
                                                  `payment.unknown`

  `status_from`,          VARCHAR(24)             Normalized statuses
  `status_to`                                     (`status_from` is null
                                                  for the first row)

  `source`                VARCHAR(16)             `CREATE`, `CALLBACK`,
                                                  `INQUIRY`, `RECONCILE`,
                                                  `EXPIRY`

  `occurred_at`,          TIMESTAMPTZ             Business and audit
  `created_at`                                    timestamps
  -----------------------------------------------------------------------

The row is inserted in the same transaction as the status change.
`webhook_deliveries` and `DELIVER_WEBHOOK` jobs are created from it for
the delivered event types (TRD 11.3).

## 4. Status and State Consistency

The transaction's normalized status is separate from:

-   Provider raw status.
-   Payment attempt outcome.
-   Webhook event processing status.
-   Webhook delivery status.
-   Job status.

These fields must not be conflated.

Recommended processing transaction for a matched, verified callback:

1.  Lock or conditionally update the payment row.
2.  Validate the incoming status transition and provider evidence.
3.  Update normalized/provider status.
4.  Mark the event processed.
5.  Insert the `payment_events` row, then the delivery row and its
    `DELIVER_WEBHOOK` job.
6.  Commit atomically.

## 5. Indexing Recommendations

-   `payment_transactions(client_id, merchant_reference)` partial
    unique where `status NOT IN ('FAILED', 'EXPIRED', 'CANCELLED')`.
-   `payment_transactions(merchant_account_id,
    provider_partner_reference)` unique.
-   `payment_transactions(merchant_account_id, protocol,
    provider_reference)` partial unique.
-   `payment_transactions(client_id, merchant_reference, created_at
    DESC)` for status-by-reference reads (a reference can have several
    payments over time).
-   `payment_transactions(status, updated_at)` for reconciliation scans
    (consider a partial index on non-final statuses).
-   `payment_attempts(payment_id, operation, attempt_no)` unique.
-   `webhook_events(event_fingerprint)` unique.
-   `webhook_events(processing_status, received_at)` partial, where
    `processing_status IN ('UNMATCHED', 'FAILED')`, for correlation
    retries and operator review.
-   `payment_events(payment_id, occurred_at)`.
-   `webhook_deliveries(payment_event_id, client_id)` unique.
-   `client_merchant_routes(client_id, route_code, payment_method,
    channel_code)` partial unique where `status = 'ACTIVE'`.
-   `client_merchant_routes(client_id, payment_method)` partial unique
    where `is_default AND status = 'ACTIVE'`.
-   `gateway_jobs(status, run_after)` partial index covering
    `PENDING`, `RETRY`, and `RUNNING` rows for claim queries.
-   `gateway_jobs(dedupe_key)` unique where not null.
-   `api_idempotency_keys(client_id, idempotency_key)` unique.
-   `client_credentials(oauth_client_id)` unique.
-   `audit_logs(entity_type, entity_id, created_at)` for investigation.

Avoid indexing large JSON payloads unless there is a proven query
requirement. Keep the index set small; every index adds write cost on
the 500 rps target path.

## 6. Data Retention and Privacy

-   Store only the provider payload fields needed for audit and
    reconciliation.
-   Do not store customer name, email, or phone after the provider call.
-   Redact authentication headers, secrets, signatures, and unnecessary
    personal information.
-   The `PURGE_RETENTION` job enforces retention and keeps the
    free-tier database within its storage limit. Proposed defaults, to
    be agreed:
    -   `webhook_events` payloads and finished `gateway_jobs`: 30 days.
    -   `api_idempotency_keys` snapshots: 30 days after resolution,
        never while `IN_PROGRESS` or `UNKNOWN`.
    -   `payment_attempts`, `webhook_deliveries`, and `audit_logs`: 12
        months.
    -   `payment_transactions` and `payment_events`: not purged (they
        are the financial record); archive later only if storage
        requires it.
-   Use encryption at rest where supported by the platform.
-   Ensure deletion or archival rules do not remove records required for
    financial reconciliation or audit obligations.

## 7. Implementation Notes

-   Target database is Neon PostgreSQL. Types above use PostgreSQL
    (`UUID`, `TIMESTAMPTZ`, `JSONB`, `TEXT[]`). Migrations are authored
    with SQLAlchemy models and Alembic; runtime code uses raw SQL, so a
    later move to another PostgreSQL host or engine needs only the
    migration history and the SQL files.
-   For money, store integer rupiah (`amount`) and never use
    floating-point arithmetic.
-   Application connections use Neon's pooled endpoint; avoid
    session-level features (advisory locks, `LISTEN/NOTIFY`, prepared
    statements) that break under transaction pooling. `FOR UPDATE SKIP
    LOCKED` is transaction scoped and is safe.
-   Before finalizing migrations, confirm the actual Faspay callback
    fields and the scope of provider-reference uniqueness for both
    protocols.
