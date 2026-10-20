"""Load test toward the 500 rps target (TRD 4.1).

    uv pip install -e ".[bench]"
    LOAD_CLIENT_ID=... LOAD_CLIENT_SECRET=... \\
      locust -f loadtest/locustfile.py --host https://payment-gateway-staging.example.com

Run it against a production-like Neon branch with Faspay sandbox (or a stub) behind it,
and record sustained rps, p95/p99 per endpoint class, error rate and the bottleneck in
TRD 4.1. Create traffic is bounded by Faspay latency, so the read mix dominates.
"""

from __future__ import annotations

import os
import uuid

from locust import HttpUser, between, task


class GatewayUser(HttpUser):
    wait_time = between(0.05, 0.3)
    token: str = ""
    refs: list[str]

    def on_start(self) -> None:
        self.refs = []
        resp = self.client.post(
            "/v1/oauth/token",
            data={"grant_type": "client_credentials"},
            auth=(os.environ["LOAD_CLIENT_ID"], os.environ["LOAD_CLIENT_SECRET"]),
            name="token",
        )
        self.token = resp.json()["access_token"]

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}

    @task(1)
    def create(self) -> None:
        reference = f"LOAD-{uuid.uuid4().hex[:12]}"
        resp = self.client.post(
            "/v1/payments",
            json={
                "merchant_reference": reference,
                "amount": 10000,
                "payment_method": os.environ.get("LOAD_METHOD", "QRIS"),
                "channel_code": os.environ.get("LOAD_CHANNEL", "715"),
            },
            headers={**self.headers, "Idempotency-Key": uuid.uuid4().hex},
            name="create",
        )
        if resp.status_code in (200, 201, 202):
            self.refs.append(reference)

    @task(8)
    def read(self) -> None:
        if self.refs:
            self.client.get(
                f"/v1/payments/by-reference/{self.refs[-1]}", headers=self.headers, name="read"
            )

    @task(1)
    def health(self) -> None:
        self.client.get("/health", name="health")
