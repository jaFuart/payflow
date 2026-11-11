"""Verify deterministic demo scenarios against the running Compose stack."""

import argparse
import asyncio
import os
from uuid import uuid4

import httpx
from smoke import check, payment_done, ready, request, wait_for_dlq


async def run(client: httpx.AsyncClient, wait_seconds: float) -> None:
    await ready(client, wait_seconds)
    stats = (await request(client, "GET", "/api/v1/stats", 200)).json()
    check(stats["demo_enabled"], "Set DEMO_ENABLED=true in .env and recreate API and consumer")
    await request(
        client,
        "POST",
        "/api/v1/demo/payments",
        401,
        json={"scenario": "success"},
        headers={"X-API-Key": "", "Idempotency-Key": str(uuid4())},
    )

    accepted = {}
    for scenario in ("success", "decline", "retry", "dlq"):
        key = f"demo-smoke-{uuid4()}"
        payload = {
            "scenario": scenario,
            "amount": "1490.00",
            "currency": "RUB",
            "description": f"Demo smoke: {scenario}",
        }
        initial = (
            await request(
                client,
                "POST",
                "/api/v1/demo/payments",
                202,
                json=payload,
                headers={"Idempotency-Key": key},
            )
        ).json()
        repeated = (
            await request(
                client,
                "POST",
                "/api/v1/demo/payments",
                202,
                json=payload,
                headers={"Idempotency-Key": key},
            )
        ).json()
        check(initial["payment_id"] == repeated["payment_id"], "Demo repeat created a new payment")
        await request(
            client,
            "POST",
            "/api/v1/demo/payments",
            409,
            json={**payload, "scenario": "decline" if scenario != "decline" else "success"},
            headers={"Idempotency-Key": key},
        )
        accepted[scenario] = initial

    completed = await asyncio.gather(
        *(
            payment_done(client, item["payment_id"], wait_seconds, dead_lettered=scenario == "dlq")
            for scenario, item in accepted.items()
        )
    )
    payments = dict(zip(accepted, completed, strict=True))
    expected_receipts = {
        "success": [204],
        "decline": [204],
        "retry": [503, 503, 204],
        "dlq": [503, 503, 503],
    }
    for scenario, payment in payments.items():
        payment_id = payment["payment_id"]
        expected_status = "failed" if scenario == "decline" else "succeeded"
        check(payment["status"] == expected_status, f"{scenario}: wrong gateway result")
        check(payment["delivery_generation"] == 0, f"{scenario}: wrong initial delivery generation")
        check(
            payment["processing_attempts"] == len(expected_receipts[scenario]),
            f"{scenario}: wrong attempt count",
        )
        receipts = (
            await request(client, "GET", f"/api/v1/payments/{payment_id}/webhooks", 200)
        ).json()["items"]
        receipts.sort(key=lambda item: item["attempt"])
        check(
            [item["status_code"] for item in receipts] == expected_receipts[scenario],
            f"{scenario}: webhook receipt history does not match scenario: {receipts}",
        )
        check(
            all(item["delivery_generation"] == 0 for item in receipts), "Wrong receipt generation"
        )
        events = (
            await request(client, "GET", f"/api/v1/payments/{payment_id}/events", 200)
        ).json()["items"]
        types = [item["event_type"] for item in events]
        check(
            types.count(f"gateway.{expected_status}") == 1,
            f"{scenario}: gateway history duplicated",
        )
        if scenario == "dlq":
            check("payment.dead_lettered" in types, "Missing dead-letter activity")
            await wait_for_dlq(payment_id, wait_seconds)
        else:
            check("webhook.delivered" in types, f"{scenario}: no delivered activity")
        if scenario == "retry":
            check(types.count("webhook.failed") == 2, "Missing webhook failure activities")
            check(types.count("retry.scheduled") == 2, "Missing persisted retry schedule")
        print(
            f"PASS: {scenario}: payment={expected_status}, HTTP={expected_receipts[scenario]}",
            flush=True,
        )

    listing = (await request(client, "GET", "/api/v1/payments?limit=100", 200)).json()
    listed_ids = {item["payment_id"] for item in listing["items"]}
    check(
        all(item["payment_id"] in listed_ids for item in payments.values()),
        "Demo payments not listed",
    )
    check(listing["total"] >= stats["total"] + 4, "Payment total did not increase")

    dead = payments["dlq"]
    payment_id = dead["payment_id"]
    await request(client, "POST", f"/api/v1/payments/{payment_id}/redeliver", 202)
    await request(client, "POST", f"/api/v1/payments/{payment_id}/redeliver", 409)
    recovered = await payment_done(client, payment_id, wait_seconds)
    for field in ("payment_id", "amount", "currency", "status", "created_at", "processed_at"):
        check(recovered[field] == dead[field], f"Redelivery changed payment field {field}")
    check(recovered["delivery_generation"] == 1, "Redelivery did not advance generation")
    check(recovered["processing_attempts"] == 1, "Recovery should deliver on the first attempt")
    check(recovered["dead_lettered_at"] is None, "Recovery retained the dead-letter marker")
    receipts = (
        await request(client, "GET", f"/api/v1/payments/{payment_id}/webhooks", 200)
    ).json()["items"]
    check(len(receipts) == 4, "Redelivery lost or duplicated webhook receipts")
    check(
        [
            (item["attempt"], item["status_code"])
            for item in receipts
            if item["delivery_generation"] == 1
        ]
        == [(1, 204)],
        "Recovery receipt has wrong generation or result",
    )
    events = (await request(client, "GET", f"/api/v1/payments/{payment_id}/events", 200)).json()[
        "items"
    ]
    types = [item["event_type"] for item in events]
    check(types.count("gateway.succeeded") == 1, "Redelivery invoked the gateway again")
    check(
        types.count("webhook.redelivery_requested") == 1, "Recovery was not recorded exactly once"
    )
    print(
        "PASS: DLQ recovery preserves gateway result and keeps both delivery generations",
        flush=True,
    )


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-base", default="http://127.0.0.1:8000")
    parser.add_argument("--timeout", type=float, default=120)
    args = parser.parse_args()
    async with httpx.AsyncClient(
        base_url=args.api_base,
        headers={"X-API-Key": os.environ["API_KEY"]},
        timeout=15,
        trust_env=False,
    ) as client:
        await run(client, args.timeout)


if __name__ == "__main__":
    asyncio.run(main())
