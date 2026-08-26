"""Application orchestration for immutable, server-authoritative quotes."""

from __future__ import annotations

import re
from collections.abc import Callable
from copy import deepcopy
from datetime import UTC, datetime, timedelta

from pydantic import ValidationError

from app.domain.canonical_json import CanonicalJSONError, canonical_json_bytes
from app.domain.enums import MerchantStatus, PurchaseType, ServiceStatus
from app.domain.exceptions import (
    InactiveMerchantError,
    InactiveServiceError,
    QuoteInputIssue,
    QuoteInputValidationError,
    QuoteIntegrityError,
    ResourceNotFoundError,
    ServiceConfigurationError,
)
from app.domain.hashing import (
    MAX_CANONICAL_INTEGER,
    calculate_quote_hash,
)
from app.domain.ids import new_quote_id
from app.domain.integrity import IntegrityStructureError
from app.domain.json_schema import (
    JSON_SCHEMA_DIALECT,
    JSONSchemaConfigurationError,
    validate_json_schema_document,
)
from app.domain.media_types import normalize_json_content_type
from app.domain.quote_integrity import verify_quote_integrity
from app.domain.service_input import (
    ServiceInputConfigurationError,
    ServiceInputValueError,
    validate_normalize_and_hash_service_input,
)
from app.models import Merchant, Quote, Service
from app.repositories.quotes import QuoteRepository
from app.repositories.services import ServiceRepository
from app.schemas.quotes import (
    QuoteCreate,
    QuoteFulfillment,
    QuoteMerchant,
    QuotePricing,
    QuoteResponse,
    QuoteService,
    QuoteServiceSnapshot,
)

Clock = Callable[[], datetime]
_CURRENCY_PATTERN = re.compile(r"^[A-Z]{3}$")


def utc_now() -> datetime:
    return datetime.now(UTC)


class QuoteApplicationService:
    """Validate, price, snapshot, hash, and persist one authoritative quote."""

    def __init__(
        self,
        service_repository: ServiceRepository,
        quote_repository: QuoteRepository,
        *,
        ttl: timedelta,
        clock: Clock = utc_now,
    ) -> None:
        if not timedelta(seconds=1) <= ttl <= timedelta(days=1):
            raise ValueError("Quote TTL must be between one second and one day")
        self._service_repository = service_repository
        self._quote_repository = quote_repository
        self._ttl = ttl
        self._clock = clock

    async def create(self, payload: QuoteCreate) -> QuoteResponse:
        row = await self._service_repository.get_with_merchant_for_quote(payload.service_id)
        if row is None:
            raise ResourceNotFoundError("Service", payload.service_id)
        merchant, service = row
        self._require_active_offer(merchant, service)
        self._validate_service_configuration(service)

        try:
            canonical_input = validate_normalize_and_hash_service_input(
                payload.input,
                service.input_schema,
            )
        except ServiceInputConfigurationError as error:
            raise ServiceConfigurationError(service.id) from error
        except ServiceInputValueError as error:
            raise QuoteInputValidationError(
                tuple(
                    QuoteInputIssue(
                        path=path,
                        keyword=keyword,
                        error_type=(
                            "quote_input_not_canonicalizable"
                            if keyword == "canonicalization"
                            else "service_input_invalid"
                        ),
                    )
                    for path, keyword in error.violations
                )
            ) from error
        input_value = canonical_input.value
        input_hash = canonical_input.input_hash

        try:
            snapshot = self._build_snapshot(merchant, service)
            snapshot_value = snapshot.model_dump(mode="json")
            canonical_json_bytes(snapshot_value)
        except (CanonicalJSONError, ValidationError, ValueError) as error:
            raise ServiceConfigurationError(service.id) from error

        issued_at = self._read_clock()
        expires_at = issued_at + self._ttl
        quote_id = new_quote_id()
        quote_hash = calculate_quote_hash(
            quote_id=quote_id,
            merchant_id=merchant.id,
            service_id=service.id,
            service_snapshot=snapshot_value,
            input_value=input_value,
            input_hash=input_hash,
            amount=service.base_price,
            currency=service.currency,
            purchase_type=service.purchase_type,
            maximum_fulfillment_seconds=service.maximum_fulfillment_seconds,
            refund_on_fulfillment_failure=service.refund_on_fulfillment_failure,
            issued_at=issued_at,
            expires_at=expires_at,
        )
        quote = Quote(
            id=quote_id,
            merchant_id=merchant.id,
            service_id=service.id,
            service_snapshot=snapshot_value,
            input=input_value,
            input_hash=input_hash,
            amount=service.base_price,
            currency=service.currency,
            purchase_type=service.purchase_type,
            maximum_fulfillment_seconds=service.maximum_fulfillment_seconds,
            refund_on_fulfillment_failure=service.refund_on_fulfillment_failure,
            issued_at=issued_at,
            expires_at=expires_at,
            quote_hash=quote_hash,
        )
        persisted = await self._quote_repository.create(quote)
        return self._to_response(persisted, now=self._read_clock())

    async def get(self, quote_id: str) -> QuoteResponse:
        quote = await self._quote_repository.get(quote_id)
        if quote is None:
            raise ResourceNotFoundError("Quote", quote_id)
        return self._to_response(quote, now=self._read_clock())

    @staticmethod
    def _require_active_offer(merchant: Merchant, service: Service) -> None:
        if merchant.status is not MerchantStatus.ACTIVE:
            raise InactiveMerchantError(merchant.id)
        if service.status is not ServiceStatus.ACTIVE:
            raise InactiveServiceError(service.id)

    @staticmethod
    def _validate_service_configuration(service: Service) -> None:
        if (
            isinstance(service.base_price, bool)
            or not isinstance(service.base_price, int)
            or not 0 <= service.base_price <= MAX_CANONICAL_INTEGER
            or not isinstance(service.currency, str)
            or _CURRENCY_PATTERN.fullmatch(service.currency) is None
            or isinstance(service.maximum_fulfillment_seconds, bool)
            or not isinstance(service.maximum_fulfillment_seconds, int)
            or not 1 <= service.maximum_fulfillment_seconds <= 86_400
            or not isinstance(service.refund_on_fulfillment_failure, bool)
            or normalize_json_content_type(service.output_content_type) is None
        ):
            raise ServiceConfigurationError(service.id)
        try:
            PurchaseType(service.purchase_type)
            validate_json_schema_document(service.input_schema)
            validate_json_schema_document(service.output_schema)
            canonical_json_bytes(service.input_schema)
            canonical_json_bytes(service.output_schema)
        except (CanonicalJSONError, JSONSchemaConfigurationError, ValueError) as error:
            raise ServiceConfigurationError(service.id) from error

    @staticmethod
    def _build_snapshot(merchant: Merchant, service: Service) -> QuoteServiceSnapshot:
        return QuoteServiceSnapshot(
            json_schema_dialect=JSON_SCHEMA_DIALECT,
            merchant=QuoteMerchant(id=merchant.id, slug=merchant.slug, name=merchant.name),
            service=QuoteService(
                id=service.id,
                slug=service.slug,
                name=service.name,
                service_type=service.service_type,
                input_schema=deepcopy(service.input_schema),
                output_schema=deepcopy(service.output_schema),
                output_content_type=service.output_content_type,
            ),
            pricing=QuotePricing(
                amount=service.base_price,
                currency=service.currency,
                purchase_type=service.purchase_type,
            ),
            fulfillment=QuoteFulfillment(
                maximum_seconds=service.maximum_fulfillment_seconds,
                refund_on_failure=service.refund_on_fulfillment_failure,
            ),
        )

    def _to_response(self, quote: Quote, *, now: datetime) -> QuoteResponse:
        try:
            snapshot = QuoteServiceSnapshot.model_validate(quote.service_snapshot)
            verification = verify_quote_integrity(quote)
        except IntegrityStructureError as error:
            raise QuoteIntegrityError(quote.id, "INTEGRITY_QUOTE_DATA_INVALID") from error
        except ValidationError as error:
            raise QuoteIntegrityError(quote.id, "INTEGRITY_QUOTE_DATA_INVALID") from error

        issued_at = self._as_utc(quote.issued_at)
        expires_at = self._as_utc(quote.expires_at)
        if now < issued_at:
            raise QuoteIntegrityError(quote.id, "INTEGRITY_QUOTE_DATA_INVALID")
        if not verification.hash_matches:
            raise QuoteIntegrityError(quote.id)
        state = "expired" if now >= expires_at else "active"
        return QuoteResponse(
            id=quote.id,
            merchant=snapshot.merchant,
            service=snapshot.service,
            input=deepcopy(quote.input),
            input_hash=quote.input_hash,
            pricing=snapshot.pricing,
            fulfillment=snapshot.fulfillment,
            issued_at=issued_at,
            expires_at=expires_at,
            state=state,
            quote_hash=quote.quote_hash,
        )

    def _read_clock(self) -> datetime:
        return self._as_utc(self._clock())

    @staticmethod
    def _as_utc(value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Quote timestamps must be timezone-aware")
        return value.astimezone(UTC)
