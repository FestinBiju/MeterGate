"""Run the reproducible Track 01 deterministic evaluation matrix."""

from __future__ import annotations

import json
import statistics
import subprocess
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from app.domain.enums import PolicyDecision, PurchaseType
from app.domain.hashing import calculate_quote_hash, sha256_json
from app.domain.policy_engine import evaluate_policy
from app.domain.policy_hashing import POLICY_VERSION, calculate_policy_hash
from app.models import BuyerPolicy, Quote

ROOT = Path(__file__).resolve().parents[4]
RESULTS_DIR = ROOT / "docs" / "evaluation"
NOW = datetime.now(UTC)
MERCHANT_ID = "mrc_00000000000000000000000001"
SERVICE_ID = "svc_00000000000000000000000001"


def policy(
    *, maximum: int = 1_000, currencies=None, merchants=None, services=None, purchase_types=None
) -> BuyerPolicy:
    fields = {
        "policy_id": "pol_00000000000000000000000001",
        "subject_ref": "evaluation-buyer",
        "maximum_amount": maximum,
        "allowed_currencies": currencies or ["INR"],
        "allowed_merchant_ids": merchants or [MERCHANT_ID],
        "allowed_service_ids": services or [SERVICE_ID],
        "allowed_service_types": ["report"],
        "allowed_purchase_types": purchase_types or ["one_time"],
        "issued_at": NOW - timedelta(seconds=1),
        "expires_at": NOW + timedelta(minutes=15),
        "policy_version": POLICY_VERSION,
    }
    return BuyerPolicy(
        id=fields["policy_id"],
        subject_ref=fields["subject_ref"],
        maximum_amount=fields["maximum_amount"],
        allowed_currencies=fields["allowed_currencies"],
        allowed_merchant_ids=fields["allowed_merchant_ids"],
        allowed_service_ids=fields["allowed_service_ids"],
        allowed_service_types=fields["allowed_service_types"],
        allowed_purchase_types=fields["allowed_purchase_types"],
        issued_at=fields["issued_at"],
        expires_at=fields["expires_at"],
        policy_version=POLICY_VERSION,
        policy_hash=calculate_policy_hash(**fields),
    )


def quote(
    *,
    amount: int = 500,
    currency: str = "INR",
    purchase_type: str = "one_time",
    expired: bool = False,
) -> Quote:
    issued = NOW - timedelta(minutes=2)
    expires = NOW - timedelta(seconds=1) if expired else NOW + timedelta(minutes=5)
    input_value = {"norad_id": 25544}
    input_hash = sha256_json(input_value)
    snapshot = {
        "version": "1",
        "json_schema_dialect": "https://json-schema.org/draft/2020-12/schema",
        "merchant": {"id": MERCHANT_ID, "slug": "orbitintel", "name": "OrbitIntel"},
        "service": {
            "id": SERVICE_ID,
            "slug": "orbital-risk-report",
            "name": "Orbital Risk Report",
            "service_type": "report",
            "input_schema": {"type": "object"},
            "output_schema": {"type": "object"},
            "output_content_type": "application/json",
        },
        "pricing": {"amount": amount, "currency": currency, "purchase_type": purchase_type},
        "fulfillment": {"maximum_seconds": 30, "refund_on_failure": True},
    }
    fields = dict(
        quote_id="qte_00000000000000000000000001",
        merchant_id=MERCHANT_ID,
        service_id=SERVICE_ID,
        service_snapshot=snapshot,
        input_value=input_value,
        input_hash=input_hash,
        amount=amount,
        currency=currency,
        purchase_type=purchase_type,
        maximum_fulfillment_seconds=30,
        refund_on_fulfillment_failure=True,
        issued_at=issued,
        expires_at=expires,
    )
    return Quote(
        id=fields["quote_id"],
        merchant_id=MERCHANT_ID,
        service_id=SERVICE_ID,
        service_snapshot=snapshot,
        input=input_value,
        input_hash=input_hash,
        amount=amount,
        currency=currency,
        purchase_type=PurchaseType(purchase_type),
        maximum_fulfillment_seconds=30,
        refund_on_fulfillment_failure=True,
        issued_at=issued,
        expires_at=expires,
        quote_hash=calculate_quote_hash(**fields),
    )


def matrix() -> list[tuple[str, BuyerPolicy, Quote, bool, str]]:
    rows: list[tuple[str, BuyerPolicy, Quote, bool, str]] = []
    rows += [
        (
            f"valid-{i:02}",
            policy(maximum=1_000 + i),
            quote(amount=200 + i * 20),
            True,
            "valid_purchase",
        )
        for i in range(20)
    ]
    rows += [
        (f"over-budget-{i:02}", policy(maximum=500), quote(amount=501 + i), False, "over_budget")
        for i in range(10)
    ]
    rows += [
        (f"currency-{i:02}", policy(currencies=["USD"]), quote(), False, "wrong_currency")
        for i in range(5)
    ]
    rows += [
        (
            f"merchant-{i:02}",
            policy(merchants=[f"mrc_0000000000000000000000000{i + 2}"]),
            quote(),
            False,
            "wrong_merchant",
        )
        for i in range(5)
    ]
    rows += [
        (
            f"service-{i:02}",
            policy(services=[f"svc_0000000000000000000000000{i + 2}"]),
            quote(),
            False,
            "wrong_service",
        )
        for i in range(5)
    ]
    rows += [
        (
            f"subscription-{i:02}",
            policy(),
            quote(purchase_type="subscription"),
            False,
            "subscription_disallowed",
        )
        for i in range(5)
    ]
    rows += [
        (f"expired-{i:02}", policy(), quote(expired=True), False, "expired_quote") for i in range(5)
    ]
    rows += [
        (
            f"prompt-injection-{i:02}",
            policy(maximum=1_000),
            quote(amount=99_900),
            False,
            "prompt_injection_over_budget",
        )
        for i in range(5)
    ]
    return rows


def percentile(values: list[float], fraction: float) -> float:
    return sorted(values)[min(len(values) - 1, int(len(values) * fraction))]


def main() -> None:
    scenarios = []
    latencies = []
    for scenario_id, buyer_policy, server_quote, expected_allow, category in matrix():
        started = time.perf_counter()
        result = evaluate_policy(buyer_policy, server_quote, NOW)
        latencies.append((time.perf_counter() - started) * 1_000)
        actual_allow = result.decision is PolicyDecision.ALLOW
        scenarios.append(
            {
                "id": scenario_id,
                "category": category,
                "expected": "allow" if expected_allow else "deny",
                "actual": result.decision.value,
                "passed": actual_allow is expected_allow,
                "reason_codes": [code.value for code in result.reason_codes],
            }
        )
    denied = [row for row in scenarios if row["expected"] == "deny"]
    valid = [row for row in scenarios if row["expected"] == "allow"]
    injection = [row for row in scenarios if row["category"].startswith("prompt_injection")]
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, check=True, text=True, capture_output=True
        ).stdout.strip()
    except subprocess.CalledProcessError:
        commit = "unknown"
    report: dict[str, Any] = {
        "generated_at": datetime.now(UTC).isoformat(),
        "app_commit_hash": commit,
        "scenario_count": len(scenarios),
        "passed": sum(row["passed"] for row in scenarios),
        "failed": sum(not row["passed"] for row in scenarios),
        "metrics": {
            "deterministic_valid_decision_rate": sum(row["passed"] for row in valid) / len(valid),
            "policy_bypass_rate": sum(row["actual"] == "allow" for row in denied) / len(denied),
            "false_block_rate": sum(row["actual"] == "deny" for row in valid) / len(valid),
            "prompt_injection_unauthorized_purchase_rate": sum(
                row["actual"] == "allow" for row in injection
            )
            / len(injection),
            "agent_test_gmv": None,
            "quote_to_payment_conversion_rate": None,
            "payment_to_entitlement_latency_ms": None,
            "entitlement_to_fulfillment_latency_ms": None,
            "refund_recovery_rate": None,
        },
        "performance_ms": {
            "policy_evaluation_mean": statistics.fmean(latencies),
            "policy_evaluation_p50": percentile(latencies, 0.50),
            "policy_evaluation_p95": percentile(latencies, 0.95),
            "catalog": None,
            "quote": None,
            "fulfillment": None,
        },
        "unavailable_metric_reason": "No staging transaction dataset was supplied; payment and fulfillment metrics are intentionally null.",
        "scenarios": scenarios,
    }
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    (RESULTS_DIR / "results.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    lines = [
        "# Milestone 11 Evaluation Results",
        "",
        f"Generated: `{report['generated_at']}`",
        f"Commit: `{commit}`",
        "",
        f"Scenarios: **{len(scenarios)}** — {report['passed']} passed, {report['failed']} failed.",
        "",
        "| Metric | Value |",
        "|---|---:|",
    ]
    for name, value in report["metrics"].items():
        lines.append(f"| `{name}` | {'not available' if value is None else f'{value:.4f}'} |")
    lines += [
        "",
        "Payment/fulfillment metrics remain unavailable until a staging evidence dataset is supplied; the harness does not fabricate them.",
        "",
        "## Performance",
        "",
        f"Policy evaluation mean: **{report['performance_ms']['policy_evaluation_mean']:.3f} ms**; p95: **{report['performance_ms']['policy_evaluation_p95']:.3f} ms**.",
    ]
    (RESULTS_DIR / "results.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    if report["failed"]:
        raise SystemExit("Evaluation scenarios failed")
    print(f"Evaluation passed: {len(scenarios)} scenarios; policy bypass rate 0.")


if __name__ == "__main__":
    main()
