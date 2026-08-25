from collections.abc import Callable
from typing import Any

import pytest
from pydantic import ValidationError

from app.domain.enums import MerchantStatus, ServiceStatus
from app.schemas.merchants import MerchantCreate, MerchantPatch
from app.schemas.services import ServiceCreate, ServicePatch


@pytest.fixture
def service_payload() -> dict[str, Any]:
    return {
        "slug": "orbital-risk-report",
        "name": "Orbital Risk Report",
        "description": "Generate a current orbital-risk analysis.",
        "service_type": "report",
        "purchase_type": "one_time",
        "currency": "INR",
        "base_price": 500,
        "input_schema": {},
        "output_schema": {"type": "object"},
        "output_content_type": "application/json",
        "maximum_fulfillment_seconds": 30,
        "refund_on_fulfillment_failure": True,
    }


def test_create_schemas_apply_only_lifecycle_defaults(
    service_payload: dict[str, Any],
) -> None:
    merchant = MerchantCreate.model_validate(
        {
            "slug": "orbitintel",
            "name": " OrbitIntel ",
            "description": " Synthetic orbital intelligence. ",
        }
    )
    service = ServiceCreate.model_validate(service_payload)

    assert merchant.name == "OrbitIntel"
    assert merchant.description == "Synthetic orbital intelligence."
    assert merchant.status is MerchantStatus.ACTIVE
    assert service.status is ServiceStatus.DRAFT
    assert service.input_schema == {}


@pytest.mark.parametrize(
    ("field_name", "invalid_value"),
    [
        ("slug", "Orbital-Risk"),
        ("currency", "inr"),
        ("currency", "USDT"),
        ("base_price", -1),
        ("base_price", True),
        ("base_price", "500"),
        ("maximum_fulfillment_seconds", 0),
        ("maximum_fulfillment_seconds", 86_401),
        ("maximum_fulfillment_seconds", 30.0),
        ("input_schema", []),
        ("input_schema", None),
        ("output_schema", "object"),
        ("output_content_type", "not-a-content-type"),
        ("status", "unknown"),
        ("unexpected", "field"),
    ],
)
def test_service_create_rejects_invalid_values(
    service_payload: dict[str, Any],
    field_name: str,
    invalid_value: object,
) -> None:
    payload = {**service_payload, field_name: invalid_value}

    with pytest.raises(ValidationError):
        ServiceCreate.model_validate(payload)


@pytest.mark.parametrize(
    "schema_type",
    [MerchantPatch, ServicePatch],
)
@pytest.mark.parametrize("payload", [{}, {"description": None}])
def test_patch_schemas_require_nonempty_nonnull_updates(
    schema_type: Callable[..., object],
    payload: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        schema_type(**payload)


def test_service_creation_requires_all_commerce_and_fulfillment_fields(
    service_payload: dict[str, Any],
) -> None:
    required_fields = (
        "service_type",
        "purchase_type",
        "currency",
        "base_price",
        "input_schema",
        "output_schema",
        "output_content_type",
        "maximum_fulfillment_seconds",
        "refund_on_fulfillment_failure",
    )

    for field_name in required_fields:
        with pytest.raises(ValidationError):
            ServiceCreate.model_validate(
                {key: value for key, value in service_payload.items() if key != field_name}
            )


def test_nonblank_descriptions_and_extra_fields_are_rejected() -> None:
    with pytest.raises(ValidationError):
        MerchantCreate.model_validate(
            {
                "slug": "orbitintel",
                "name": "OrbitIntel",
                "description": "   ",
            }
        )

    with pytest.raises(ValidationError):
        MerchantCreate.model_validate(
            {
                "slug": "orbitintel",
                "name": "OrbitIntel",
                "description": "Synthetic merchant.",
                "secret": "must-not-be-accepted",
            }
        )
