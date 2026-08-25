from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from app.domain.canonical_json import CanonicalJSONError, canonical_json_bytes
from app.domain.enums import MerchantStatus, PurchaseType, ServiceStatus, ServiceType
from app.domain.exceptions import (
    InactiveMerchantError,
    InactiveServiceError,
    QuoteInputValidationError,
    ResourceNotFoundError,
    ServiceConfigurationError,
)
from app.domain.hashing import (
    MAX_CANONICAL_INTEGER,
    calculate_quote_hash,
    recompute_quote_integrity,
    sha256_json,
)
from app.domain.json_schema import (
    MAX_INSTANCE_TEXT_BYTES,
    JSONSchemaConfigurationError,
    validate_json_instance,
)
from app.models import Merchant, Quote, Service
from app.repositories.quotes import QuoteRepository
from app.schemas.quotes import QuoteCreate
from app.services.quotes import QuoteApplicationService


@dataclass
class FrozenClock:
    current: datetime

    def __call__(self) -> datetime:
        return self.current


class FakeServiceRepository:
    def __init__(self, row: tuple[Merchant, Service] | None) -> None:
        self.row = row

    async def get_with_merchant_for_quote(
        self,
        service_id: str,
    ) -> tuple[Merchant, Service] | None:
        if self.row is None or self.row[1].id != service_id:
            return None
        return self.row


class FakeQuoteRepository:
    def __init__(self) -> None:
        self.records: dict[str, Quote] = {}

    async def create(self, quote: Quote) -> Quote:
        self.records[quote.id] = quote
        return quote

    async def get(self, quote_id: str) -> Quote | None:
        return self.records.get(quote_id)


class AdvancingQuoteRepository(FakeQuoteRepository):
    def __init__(self, clock: FrozenClock) -> None:
        super().__init__()
        self._clock = clock

    async def create(self, quote: Quote) -> Quote:
        persisted = await super().create(quote)
        self._clock.current = quote.expires_at
        return persisted


def make_offer(
    *,
    merchant_status: MerchantStatus = MerchantStatus.ACTIVE,
    service_status: ServiceStatus = ServiceStatus.ACTIVE,
    input_schema: dict[str, Any] | None = None,
    price: int = 500,
) -> tuple[Merchant, Service]:
    merchant = Merchant(
        id="mrc_00000000000000000000000001",
        slug="orbitintel",
        name="OrbitIntel",
        description="Synthetic orbital intelligence.",
        status=merchant_status,
    )
    service = Service(
        id="svc_00000000000000000000000001",
        merchant_id=merchant.id,
        slug="orbital-risk-report",
        name="Orbital Risk Report",
        description="Generate a synthetic risk report.",
        status=service_status,
        service_type=ServiceType.REPORT,
        purchase_type=PurchaseType.ONE_TIME,
        currency="INR",
        base_price=price,
        input_schema=input_schema
        or {
            "type": "object",
            "properties": {"norad_id": {"type": "integer"}},
            "required": ["norad_id"],
            "additionalProperties": False,
        },
        output_schema={"type": "object"},
        output_content_type="application/json",
        maximum_fulfillment_seconds=30,
        refund_on_fulfillment_failure=True,
    )
    return merchant, service


def make_application(
    offer: tuple[Merchant, Service] | None,
    *,
    clock: FrozenClock | None = None,
) -> tuple[QuoteApplicationService, FakeQuoteRepository, FrozenClock]:
    effective_clock = clock or FrozenClock(datetime(2026, 8, 25, 12, tzinfo=UTC))
    quote_repository = FakeQuoteRepository()
    application = QuoteApplicationService(
        FakeServiceRepository(offer),  # type: ignore[arg-type]
        quote_repository,  # type: ignore[arg-type]
        ttl=timedelta(minutes=5),
        clock=effective_clock,
    )
    return application, quote_repository, effective_clock


def test_rfc8785_canonicalization_is_recursive_order_independent_and_typed() -> None:
    first = {"z": {"b": 2, "a": 1}, "array": [True, None, 1.0], "text": "é"}
    second = {"text": "é", "array": [True, None, 1.0], "z": {"a": 1, "b": 2}}

    assert canonical_json_bytes(first) == canonical_json_bytes(second)
    assert sha256_json(first) == sha256_json(second)
    assert canonical_json_bytes({"b": 2, "a": 1}) == b'{"a":1,"b":2}'
    assert sha256_json([1, 2]) != sha256_json([2, 1])


@pytest.mark.parametrize("value", [float("nan"), float("inf"), 1 << 53])
def test_rfc8785_rejects_non_interoperable_numbers(value: float | int) -> None:
    with pytest.raises(CanonicalJSONError):
        canonical_json_bytes(value)


def test_json_schema_validation_supports_nested_and_general_json_roots() -> None:
    schema = {
        "type": "object",
        "properties": {
            "target": {
                "type": "object",
                "properties": {"norad_id": {"type": "integer"}},
                "required": ["norad_id"],
                "additionalProperties": False,
            }
        },
        "required": ["target"],
        "additionalProperties": False,
    }

    assert validate_json_instance({"target": {"norad_id": 25544}}, schema) == ()
    issues = validate_json_instance({"target": {"norad_id": "ISS", "extra": True}}, schema)
    assert {issue.keyword for issue in issues} == {"additionalProperties", "type"}
    assert validate_json_instance(None, {"type": "null"}) == ()
    assert validate_json_instance([1, 2], {"type": "array", "items": {"type": "integer"}}) == ()


def test_json_schema_validation_bounds_errors_and_input_complexity() -> None:
    required = [f"field_{index}" for index in range(100)]
    issues = validate_json_instance({}, {"type": "object", "required": required})
    oversized = validate_json_instance(
        "x" * (MAX_INSTANCE_TEXT_BYTES + 1),
        {"type": "string"},
    )

    assert len(issues) == 20
    assert len(oversized) == 1
    assert oversized[0].path == ()
    assert oversized[0].keyword == "complexity"


@pytest.mark.parametrize(
    "schema",
    [
        {"type": "string", "pattern": "^(a+)+$"},
        {
            "type": "object",
            "patternProperties": {"^(a+)+$": {"type": "string"}},
        },
    ],
)
def test_json_schema_rejects_regex_keywords_before_evaluating_input(
    schema: dict[str, Any],
) -> None:
    with pytest.raises(JSONSchemaConfigurationError, match="Regex JSON Schema"):
        validate_json_instance("a" * 100 + "!", schema)


def test_json_schema_allows_a_property_named_pattern() -> None:
    schema = {
        "type": "object",
        "properties": {"pattern": {"type": "string"}},
        "required": ["pattern"],
        "additionalProperties": False,
    }

    assert validate_json_instance({"pattern": "ordinary value"}, schema) == ()


def test_json_schema_rejects_excessive_schema_depth() -> None:
    schema: dict[str, Any] = {"type": "string"}
    for _ in range(40):
        schema = {"not": schema}

    with pytest.raises(JSONSchemaConfigurationError, match="complexity limits"):
        validate_json_instance("value", schema)


@pytest.mark.parametrize(
    "schema",
    [
        {"$ref": "https://schemas.example/input.json"},
        {"$ref": ""},
        {"$ref": "#"},
        {"$schema": "http://json-schema.org/draft-07/schema#"},
    ],
)
def test_json_schema_rejects_remote_unsupported_and_recursive_root_references(
    schema: dict[str, Any],
) -> None:
    with pytest.raises(JSONSchemaConfigurationError):
        validate_json_instance({}, schema)


def quote_hash_fields(**overrides: Any) -> dict[str, Any]:
    issued_at = datetime(2026, 8, 25, 12, tzinfo=UTC)
    fields: dict[str, Any] = {
        "quote_id": "qte_00000000000000000000000001",
        "merchant_id": "mrc_00000000000000000000000001",
        "service_id": "svc_00000000000000000000000001",
        "service_snapshot": {"version": "1", "service": {"id": "svc_1"}},
        "input_value": {"norad_id": 25544},
        "input_hash": sha256_json({"norad_id": 25544}),
        "amount": 500,
        "currency": "INR",
        "purchase_type": PurchaseType.ONE_TIME,
        "maximum_fulfillment_seconds": 30,
        "refund_on_fulfillment_failure": True,
        "issued_at": issued_at,
        "expires_at": issued_at + timedelta(minutes=5),
    }
    fields.update(overrides)
    return fields


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("amount", 501),
        ("input_value", {"norad_id": 25545}),
        ("input_hash", f"sha256:{'1' * 64}"),
        ("merchant_id", "mrc_00000000000000000000000002"),
        ("service_id", "svc_00000000000000000000000002"),
        ("maximum_fulfillment_seconds", 31),
        ("refund_on_fulfillment_failure", False),
    ],
)
def test_quote_hash_binds_every_commercial_dimension(field: str, replacement: Any) -> None:
    original = quote_hash_fields()
    changed = {**original, field: replacement}

    assert calculate_quote_hash(**original) != calculate_quote_hash(**changed)


@pytest.mark.asyncio
async def test_quote_creation_is_authoritative_normalized_fresh_and_snapshot_backed() -> None:
    merchant, service = make_offer(price=725)
    application, repository, clock = make_application((merchant, service))
    payload = QuoteCreate(service_id=service.id, input={"value": -0.0, "count": 1.0})
    service.input_schema = {
        "type": "object",
        "properties": {
            "value": {"type": "number"},
            "count": {"type": "integer"},
        },
        "required": ["value", "count"],
        "additionalProperties": False,
    }

    first = await application.create(payload)
    second = await application.create(payload)

    assert first.id.startswith("qte_")
    assert second.id != first.id
    assert second.quote_hash != first.quote_hash
    assert first.input == {"count": 1, "value": 0}
    assert first.pricing.model_dump(mode="json") == {
        "amount": 725,
        "currency": "INR",
        "purchase_type": "one_time",
    }
    assert first.expires_at - first.issued_at == timedelta(minutes=5)
    persisted = repository.records[first.id]
    assert recompute_quote_integrity(persisted).input_hash == persisted.input_hash
    assert recompute_quote_integrity(persisted).quote_hash == persisted.quote_hash

    service.name = "Changed current service"
    service.base_price = 999
    service.maximum_fulfillment_seconds = 90
    clock.current = first.expires_at
    historical = await application.get(first.id)
    assert historical.service.name == "Orbital Risk Report"
    assert historical.pricing.amount == 725
    assert historical.fulfillment.maximum_seconds == 30
    assert historical.state == "expired"


@pytest.mark.asyncio
async def test_quote_supports_root_null_when_the_schema_allows_it() -> None:
    merchant, service = make_offer(input_schema={"type": "null"})
    application, _, _ = make_application((merchant, service))

    quote = await application.create(QuoteCreate(service_id=service.id, input=None))

    assert quote.input is None


@pytest.mark.asyncio
async def test_quote_creation_derives_state_after_persistence() -> None:
    merchant, service = make_offer()
    clock = FrozenClock(datetime(2026, 8, 25, 12, tzinfo=UTC))
    repository = AdvancingQuoteRepository(clock)
    application = QuoteApplicationService(
        FakeServiceRepository((merchant, service)),  # type: ignore[arg-type]
        repository,  # type: ignore[arg-type]
        ttl=timedelta(seconds=1),
        clock=clock,
    )

    quote = await application.create(QuoteCreate(service_id=service.id, input={"norad_id": 25544}))

    assert quote.state == "expired"


@pytest.mark.asyncio
async def test_quote_rejects_unknown_inactive_invalid_input_and_unsafe_price() -> None:
    missing_application, _, _ = make_application(None)
    with pytest.raises(ResourceNotFoundError):
        await missing_application.create(QuoteCreate(service_id="svc_missing", input={}))

    merchant, service = make_offer(merchant_status=MerchantStatus.SUSPENDED)
    application, _, _ = make_application((merchant, service))
    with pytest.raises(InactiveMerchantError):
        await application.create(QuoteCreate(service_id=service.id, input={"norad_id": 25544}))

    merchant, service = make_offer(service_status=ServiceStatus.ARCHIVED)
    application, _, _ = make_application((merchant, service))
    with pytest.raises(InactiveServiceError):
        await application.create(QuoteCreate(service_id=service.id, input={"norad_id": 25544}))

    merchant, service = make_offer()
    application, _, _ = make_application((merchant, service))
    with pytest.raises(QuoteInputValidationError) as error:
        await application.create(QuoteCreate(service_id=service.id, input={"norad_id": "ISS"}))
    assert error.value.issues[0].keyword == "type"

    merchant, service = make_offer(price=MAX_CANONICAL_INTEGER + 1)
    application, _, _ = make_application((merchant, service))
    with pytest.raises(ServiceConfigurationError):
        await application.create(QuoteCreate(service_id=service.id, input={"norad_id": 25544}))


@pytest.mark.asyncio
async def test_malformed_self_referencing_stored_schema_is_a_configuration_conflict() -> None:
    merchant, service = make_offer(input_schema={"$ref": ""})
    application, _, _ = make_application((merchant, service))

    with pytest.raises(ServiceConfigurationError):
        await application.create(QuoteCreate(service_id=service.id, input={}))


def test_quote_repository_surface_has_no_mutation_methods() -> None:
    assert not hasattr(QuoteRepository, "update")
    assert not hasattr(QuoteRepository, "delete")


def test_quote_service_rejects_subsecond_or_overlong_ttl() -> None:
    service_repository = FakeServiceRepository(None)
    quote_repository = FakeQuoteRepository()

    with pytest.raises(ValueError):
        QuoteApplicationService(  # type: ignore[arg-type]
            service_repository,
            quote_repository,
            ttl=timedelta(microseconds=1),
        )
    with pytest.raises(ValueError):
        QuoteApplicationService(  # type: ignore[arg-type]
            service_repository,
            quote_repository,
            ttl=timedelta(days=1, seconds=1),
        )


def test_recomputed_quote_hash_changes_when_persisted_input_is_mutated() -> None:
    fields = quote_hash_fields()
    quote = SimpleNamespace(
        id=fields["quote_id"],
        merchant_id=fields["merchant_id"],
        service_id=fields["service_id"],
        service_snapshot=fields["service_snapshot"],
        input=fields["input_value"],
        input_hash=fields["input_hash"],
        amount=fields["amount"],
        currency=fields["currency"],
        purchase_type=fields["purchase_type"],
        maximum_fulfillment_seconds=fields["maximum_fulfillment_seconds"],
        refund_on_fulfillment_failure=fields["refund_on_fulfillment_failure"],
        issued_at=fields["issued_at"],
        expires_at=fields["expires_at"],
    )
    original = recompute_quote_integrity(quote)
    quote.input = {"norad_id": 25545}

    changed = recompute_quote_integrity(quote)

    assert changed.input_hash != original.input_hash
    assert changed.quote_hash != original.quote_hash
