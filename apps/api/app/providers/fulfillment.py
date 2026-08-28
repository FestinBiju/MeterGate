"""Narrow authenticated HTTP adapter for private merchant fulfillment."""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from hmac import compare_digest
from typing import Any, Protocol, runtime_checkable
from urllib.parse import quote

import httpx

from app.domain.media_types import normalize_json_content_type

_FULFILLMENT_ID = re.compile(r"^ful_[0-7][0-9A-HJKMNP-TV-Z]{25}$")
_SERVICE_ID = re.compile(r"^svc_[0-7][0-9A-HJKMNP-TV-Z]{25}$")
_HASH = re.compile(r"^sha256:[0-9a-f]{64}$")
_SLUG = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_MERCHANT_INTEGRITY_CODES = frozenset(
    {
        "ORBITINTEL_EXECUTION_UNCERTAIN",
        "ORBITINTEL_IDEMPOTENCY_MISMATCH",
        "ORBITINTEL_IDEMPOTENCY_INTEGRITY_FAILED",
        "ORBITINTEL_EXECUTION_LEASE_LOST",
        "ORBITINTEL_RESULT_SIZE_INVALID",
        "ORBITINTEL_INPUT_HASH_MISMATCH",
    }
)


class MerchantFulfillmentProviderError(RuntimeError):
    """Sanitized failure at the private merchant network boundary."""

    def __init__(self, reason_code: str, message: str) -> None:
        self.reason_code = reason_code
        super().__init__(message)


class MerchantFulfillmentRetryableError(MerchantFulfillmentProviderError):
    """Transport or merchant state permits a bounded same-key retry."""


class MerchantFulfillmentInProgressError(MerchantFulfillmentProviderError):
    """The same logical merchant execution is still owned by another request."""


class MerchantFulfillmentPermanentError(MerchantFulfillmentProviderError):
    """The merchant definitively rejected this immutable execution."""


class MerchantFulfillmentResponseError(MerchantFulfillmentProviderError):
    """The merchant response cannot cross the value-release boundary."""


@dataclass(frozen=True, slots=True)
class MerchantFulfillmentRequest:
    fulfillment_execution_id: str
    service_id: str
    service_slug: str
    input: Any
    input_hash: str
    request_timeout_seconds: float = 30.0

    def __post_init__(self) -> None:
        if _FULFILLMENT_ID.fullmatch(self.fulfillment_execution_id) is None:
            raise ValueError("Fulfillment execution ID is invalid")
        if _SERVICE_ID.fullmatch(self.service_id) is None:
            raise ValueError("Fulfillment service ID is invalid")
        if _SLUG.fullmatch(self.service_slug) is None:
            raise ValueError("Fulfillment service slug is invalid")
        if _HASH.fullmatch(self.input_hash) is None:
            raise ValueError("Fulfillment input hash is invalid")
        if not 0 < self.request_timeout_seconds <= 900:
            raise ValueError("Fulfillment hard request timeout is invalid")


@dataclass(frozen=True, slots=True)
class MerchantFulfillmentResult:
    result_content_type: str
    result: Any


@runtime_checkable
class FulfillmentProvider(Protocol):
    async def execute(
        self,
        request: MerchantFulfillmentRequest,
        *,
        endpoint_path: str,
    ) -> MerchantFulfillmentResult: ...


class HttpMerchantFulfillmentProvider:
    """Call one configured private merchant with a stable logical execution ID."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        base_url: str,
        shared_secret: str,
        maximum_response_bytes: int,
    ) -> None:
        if not isinstance(client, httpx.AsyncClient):
            raise ValueError("An async HTTP client is required")
        if not base_url or base_url.endswith("/"):
            raise ValueError("Merchant base URL must be normalized without a trailing slash")
        if len(shared_secret.encode("utf-8")) < 32:
            raise ValueError("Merchant shared secret must contain at least 32 bytes")
        if type(maximum_response_bytes) is not int or not 1_024 <= maximum_response_bytes <= (
            1_048_576
        ):
            raise ValueError("Merchant response limit is invalid")
        self._client = client
        self._base_url = base_url
        self._shared_secret = shared_secret
        self._maximum_response_bytes = maximum_response_bytes

    async def execute(
        self,
        request: MerchantFulfillmentRequest,
        *,
        endpoint_path: str,
    ) -> MerchantFulfillmentResult:
        if not isinstance(request, MerchantFulfillmentRequest):
            raise ValueError("A validated fulfillment request is required")
        if (
            not isinstance(endpoint_path, str)
            or not endpoint_path.startswith("/")
            or endpoint_path.endswith("/")
            or "?" in endpoint_path
            or "#" in endpoint_path
            or ".." in endpoint_path.split("/")
        ):
            raise MerchantFulfillmentResponseError(
                "FULFILLMENT_PROVIDER_CONFIGURATION_INVALID",
                "Merchant endpoint configuration is invalid",
            )
        url = f"{self._base_url}{endpoint_path}/{quote(request.fulfillment_execution_id, safe='')}"
        try:
            async with asyncio.timeout(request.request_timeout_seconds):
                async with self._client.stream(
                    "POST",
                    url,
                    headers={
                        "Accept": "application/json",
                        "Accept-Encoding": "identity",
                        "Authorization": f"Bearer {self._shared_secret}",
                    },
                    json={
                        "service_id": request.service_id,
                        "service_slug": request.service_slug,
                        "input": request.input,
                        "input_hash": request.input_hash,
                    },
                ) as response:
                    raw = await self._read_bounded(response)
        except (TimeoutError, httpx.TimeoutException) as error:
            raise MerchantFulfillmentRetryableError(
                "FULFILLMENT_PROVIDER_UNAVAILABLE",
                "Merchant fulfillment timed out",
            ) from error
        except httpx.TransportError as error:
            raise MerchantFulfillmentRetryableError(
                "FULFILLMENT_PROVIDER_UNAVAILABLE",
                "Merchant fulfillment is unavailable",
            ) from error

        if response.status_code == 200:
            body = self._parse_object(raw)
            return self._parse_success(body, request)
        safe_code = self._parse_optional_reason_code(raw)
        if safe_code in _MERCHANT_INTEGRITY_CODES:
            raise MerchantFulfillmentResponseError(
                safe_code,
                "Merchant fulfillment returned contradictory execution evidence",
            )
        if response.status_code == 409 and safe_code == "ORBITINTEL_EXECUTION_IN_PROGRESS":
            raise MerchantFulfillmentInProgressError(
                safe_code,
                "Merchant fulfillment is already executing this request",
            )
        if response.status_code in {409, 425, 429} or response.status_code >= 500:
            raise MerchantFulfillmentRetryableError(
                safe_code or "FULFILLMENT_PROVIDER_UNAVAILABLE",
                "Merchant fulfillment may be retried with the same execution ID",
            )
        raise MerchantFulfillmentPermanentError(
            safe_code or "FULFILLMENT_PROVIDER_REJECTED",
            "Merchant fulfillment rejected the immutable request",
        )

    async def _read_bounded(self, response: httpx.Response) -> bytes:
        content_encodings = response.headers.get_list("content-encoding")
        if content_encodings and (
            len(content_encodings) != 1 or content_encodings[0].strip().lower() != "identity"
        ):
            raise MerchantFulfillmentResponseError(
                "FULFILLMENT_PROVIDER_RESPONSE_INVALID",
                "Encoded merchant responses are not accepted",
            )
        declared = response.headers.get("content-length")
        if declared is not None:
            try:
                declared_size = int(declared)
            except ValueError as error:
                raise MerchantFulfillmentResponseError(
                    "FULFILLMENT_PROVIDER_RESPONSE_INVALID",
                    "Merchant response length is invalid",
                ) from error
            if declared_size < 0 or declared_size > self._maximum_response_bytes:
                raise MerchantFulfillmentResponseError(
                    "FULFILLMENT_PROVIDER_RESPONSE_INVALID",
                    "Merchant response exceeds the configured size limit",
                )
        body = bytearray()
        if response.is_stream_consumed:
            chunks = (response.content,)
        else:
            chunks = response.aiter_raw()
        for_aiter = chunks
        if isinstance(for_aiter, tuple):
            for chunk in for_aiter:
                body.extend(chunk)
                if len(body) > self._maximum_response_bytes:
                    raise MerchantFulfillmentResponseError(
                        "FULFILLMENT_PROVIDER_RESPONSE_INVALID",
                        "Merchant response exceeds the configured size limit",
                    )
            return bytes(body)
        async for chunk in for_aiter:
            body.extend(chunk)
            if len(body) > self._maximum_response_bytes:
                raise MerchantFulfillmentResponseError(
                    "FULFILLMENT_PROVIDER_RESPONSE_INVALID",
                    "Merchant response exceeds the configured size limit",
                )
        return bytes(body)

    @staticmethod
    def _parse_object(raw: bytes) -> dict[str, Any]:
        try:
            value = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
            raise MerchantFulfillmentResponseError(
                "FULFILLMENT_PROVIDER_RESPONSE_INVALID",
                "Merchant response is not valid JSON",
            ) from error
        if not isinstance(value, dict):
            raise MerchantFulfillmentResponseError(
                "FULFILLMENT_PROVIDER_RESPONSE_INVALID",
                "Merchant response must be a JSON object",
            )
        return value

    @classmethod
    def _parse_optional_reason_code(cls, raw: bytes) -> str | None:
        """Extract safe error metadata without changing HTTP failure classification."""
        try:
            body = cls._parse_object(raw)
        except MerchantFulfillmentResponseError:
            return None
        reason_code = body.get("reason_code")
        if (
            not isinstance(reason_code, str)
            or re.fullmatch(r"[A-Z][A-Z0-9_]{0,99}", reason_code) is None
        ):
            return None
        return reason_code

    @staticmethod
    def _parse_success(
        body: dict[str, Any],
        request: MerchantFulfillmentRequest,
    ) -> MerchantFulfillmentResult:
        if set(body) != {
            "fulfillment_execution_id",
            "service_id",
            "input_hash",
            "result_content_type",
            "result",
        }:
            raise MerchantFulfillmentResponseError(
                "FULFILLMENT_PROVIDER_RESPONSE_INVALID",
                "Merchant success response has unexpected fields",
            )
        if (
            body["fulfillment_execution_id"] != request.fulfillment_execution_id
            or body["service_id"] != request.service_id
            or not isinstance(body["input_hash"], str)
            or not compare_digest(body["input_hash"], request.input_hash)
        ):
            raise MerchantFulfillmentResponseError(
                "FULFILLMENT_PROVIDER_RESPONSE_INVALID",
                "Merchant response references a different request binding",
            )
        result_content_type = normalize_json_content_type(body["result_content_type"])
        if result_content_type is None:
            raise MerchantFulfillmentResponseError(
                "FULFILLMENT_PROVIDER_RESPONSE_INVALID",
                "Merchant result is not a supported JSON document",
            )
        return MerchantFulfillmentResult(
            result_content_type=result_content_type,
            result=body["result"],
        )


class UnavailableFulfillmentProvider:
    """Auditable fail-closed provider used when runtime merchant wiring is absent."""

    async def execute(
        self,
        request: MerchantFulfillmentRequest,
        *,
        endpoint_path: str,
    ) -> MerchantFulfillmentResult:
        del request, endpoint_path
        raise MerchantFulfillmentRetryableError(
            "FULFILLMENT_PROVIDER_UNAVAILABLE",
            "The merchant fulfillment provider is unavailable",
        )
