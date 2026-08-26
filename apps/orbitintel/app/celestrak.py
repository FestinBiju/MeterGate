"""Bounded asynchronous client for CelesTrak's structured GP JSON endpoint."""

from __future__ import annotations

import asyncio
import json
import math
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from typing import Any, Protocol

import httpx2

from app.errors import (
    CelesTrakNotFoundError,
    CelesTrakResponseError,
    CelesTrakUnavailableError,
)
from app.schemas import GPSnapshot

CELESTRAK_GP_URL = "https://celestrak.org/NORAD/elements/gp.php"


class GPProvider(Protocol):
    async def fetch(self, norad_id: int) -> GPSnapshot: ...


class _RetryableUpstreamError(RuntimeError):
    def __init__(self, *, retry_after_seconds: float | None = None) -> None:
        self.retry_after_seconds = retry_after_seconds
        super().__init__("CelesTrak request is temporarily unavailable")


class CelesTrakClient:
    """Fetch one exact catalog record without following redirects or unbounded reads."""

    def __init__(
        self,
        client: httpx2.AsyncClient,
        *,
        maximum_response_bytes: int,
        maximum_attempts: int,
        retry_base_delay_seconds: float,
        request_timeout_seconds: float,
        clock: Callable[[], datetime] | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if maximum_response_bytes < 1:
            raise ValueError("CelesTrak response limit must be positive")
        if not 1 <= maximum_attempts <= 4:
            raise ValueError("CelesTrak attempts must be between one and four")
        if retry_base_delay_seconds < 0:
            raise ValueError("CelesTrak retry delay cannot be negative")
        if not 0 < request_timeout_seconds <= 120:
            raise ValueError("CelesTrak hard request timeout must be between 0 and 120 seconds")
        self._client = client
        self._maximum_response_bytes = maximum_response_bytes
        self._maximum_attempts = maximum_attempts
        self._retry_base_delay_seconds = retry_base_delay_seconds
        self._request_timeout_seconds = request_timeout_seconds
        self._clock = clock or (lambda: datetime.now(UTC))
        self._sleep = sleep

    async def fetch(self, norad_id: int) -> GPSnapshot:
        if type(norad_id) is not int or not 1 <= norad_id <= 999_999_999:
            raise ValueError("NORAD catalog ID must be an integer from 1 to 999999999")
        for attempt in range(self._maximum_attempts):
            try:
                async with asyncio.timeout(self._request_timeout_seconds):
                    return await self._fetch_once(norad_id)
            except (TimeoutError, _RetryableUpstreamError) as error:
                if attempt + 1 >= self._maximum_attempts:
                    raise CelesTrakUnavailableError(
                        "CelesTrak is temporarily unavailable",
                        "CELESTRAK_UNAVAILABLE",
                    ) from error
                exponential_delay = self._retry_base_delay_seconds * (2**attempt)
                retry_after = (
                    error.retry_after_seconds or 0.0
                    if isinstance(error, _RetryableUpstreamError)
                    else 0.0
                )
                await self._sleep(max(exponential_delay, retry_after))
        raise AssertionError("CelesTrak retry loop exhausted unexpectedly")

    async def _fetch_once(self, norad_id: int) -> GPSnapshot:
        try:
            async with self._client.stream(
                "GET",
                CELESTRAK_GP_URL,
                params={"CATNR": str(norad_id), "FORMAT": "JSON"},
                headers={"Accept-Encoding": "identity"},
                follow_redirects=False,
            ) as response:
                if response.status_code == 429 or 500 <= response.status_code <= 599:
                    raise _RetryableUpstreamError(
                        retry_after_seconds=_retry_after_seconds(response.headers)
                    )
                if 300 <= response.status_code <= 399:
                    raise CelesTrakResponseError(
                        "CelesTrak returned an unexpected redirect",
                        "CELESTRAK_REDIRECT_REJECTED",
                    )
                if 400 <= response.status_code <= 499:
                    raise CelesTrakResponseError(
                        "CelesTrak rejected the catalog request",
                        "CELESTRAK_REQUEST_REJECTED",
                    )
                if response.status_code != 200:
                    raise CelesTrakResponseError(
                        "CelesTrak returned an unsupported HTTP status",
                        "CELESTRAK_RESPONSE_INVALID",
                    )
                _validate_content_headers(
                    response.headers,
                    maximum_response_bytes=self._maximum_response_bytes,
                )
                body = bytearray()
                if response.is_stream_consumed:
                    chunks = (response.content,)
                else:
                    chunks = response.aiter_raw()
                if isinstance(chunks, tuple):
                    for chunk in chunks:
                        body.extend(chunk)
                        if len(body) > self._maximum_response_bytes:
                            raise CelesTrakResponseError(
                                "CelesTrak response exceeded the configured limit",
                                "CELESTRAK_RESPONSE_TOO_LARGE",
                            )
                    return parse_gp_response(
                        bytes(body), norad_id=norad_id, fetched_at=self._clock()
                    )
                async for chunk in chunks:
                    body.extend(chunk)
                    if len(body) > self._maximum_response_bytes:
                        raise CelesTrakResponseError(
                            "CelesTrak response exceeded the configured limit",
                            "CELESTRAK_RESPONSE_TOO_LARGE",
                        )
        except CelesTrakResponseError:
            raise
        except _RetryableUpstreamError:
            raise
        except (httpx2.TimeoutException, httpx2.TransportError) as error:
            raise _RetryableUpstreamError() from error
        return parse_gp_response(bytes(body), norad_id=norad_id, fetched_at=self._clock())


def parse_gp_response(body: bytes, *, norad_id: int, fetched_at: datetime) -> GPSnapshot:
    """Strictly parse exactly one GP record and retain only bounded source fields."""
    try:
        decoded = body.decode("utf-8")
        raw = json.loads(
            decoded,
            object_pairs_hook=_reject_duplicate_members,
            parse_constant=_reject_nonfinite_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise CelesTrakResponseError(
            "CelesTrak returned invalid JSON",
            "CELESTRAK_RESPONSE_INVALID",
        ) from error
    if not isinstance(raw, list):
        raise CelesTrakResponseError(
            "CelesTrak response must be a JSON array",
            "CELESTRAK_RESPONSE_INVALID",
        )
    if not raw:
        raise CelesTrakNotFoundError(
            "CelesTrak has no GP record for this NORAD catalog ID",
            "CELESTRAK_OBJECT_NOT_FOUND",
        )
    if len(raw) != 1 or not isinstance(raw[0], Mapping):
        raise CelesTrakResponseError(
            "CelesTrak did not return exactly one GP record",
            "CELESTRAK_RESPONSE_INVALID",
        )
    record = raw[0]
    returned_norad_id = _required_integer(record, "NORAD_CAT_ID", minimum=1)
    if returned_norad_id != norad_id:
        raise CelesTrakResponseError(
            "CelesTrak returned a different NORAD catalog ID",
            "CELESTRAK_RESPONSE_MISMATCH",
        )
    return GPSnapshot(
        norad_id=returned_norad_id,
        object_name=_required_string(record, "OBJECT_NAME", maximum_length=256),
        international_designator=_optional_string(
            record,
            "OBJECT_ID",
            maximum_length=64,
        ),
        epoch=_required_epoch(record, "EPOCH"),
        mean_motion=_required_number(record, "MEAN_MOTION", minimum=0, maximum=100),
        eccentricity=_required_number(
            record,
            "ECCENTRICITY",
            minimum=0,
            maximum=0.999999999999,
        ),
        inclination=_required_number(record, "INCLINATION", minimum=0, maximum=180),
        raan=_required_number(record, "RA_OF_ASC_NODE", minimum=0, maximum=360),
        argument_of_pericenter=_required_number(
            record,
            "ARG_OF_PERICENTER",
            minimum=0,
            maximum=360,
        ),
        mean_anomaly=_required_number(record, "MEAN_ANOMALY", minimum=0, maximum=360),
        classification_type=_optional_string(
            record,
            "CLASSIFICATION_TYPE",
            maximum_length=8,
        ),
        element_set_number=_optional_integer(record, "ELEMENT_SET_NO", minimum=0),
        revolution_number_at_epoch=_optional_integer(
            record,
            "REV_AT_EPOCH",
            minimum=0,
        ),
        bstar=_optional_number(record, "BSTAR"),
        mean_motion_dot=_optional_number(record, "MEAN_MOTION_DOT"),
        mean_motion_ddot=_optional_number(record, "MEAN_MOTION_DDOT"),
        fetched_at=_aware_utc(fetched_at),
    )


def _validate_content_headers(
    headers: httpx2.Headers,
    *,
    maximum_response_bytes: int,
) -> None:
    content_encodings = headers.get_list("content-encoding")
    if content_encodings and (
        len(content_encodings) != 1 or content_encodings[0].strip().lower() != "identity"
    ):
        raise CelesTrakResponseError(
            "CelesTrak returned an encoded response",
            "CELESTRAK_RESPONSE_INVALID",
        )
    content_types = headers.get_list("content-type")
    if len(content_types) != 1 or content_types[0].split(";", 1)[0].strip().lower() != (
        "application/json"
    ):
        raise CelesTrakResponseError(
            "CelesTrak response has an invalid content type",
            "CELESTRAK_RESPONSE_INVALID",
        )
    lengths = headers.get_list("content-length")
    if lengths:
        if len(lengths) != 1 or not lengths[0].isdigit():
            raise CelesTrakResponseError(
                "CelesTrak response has an invalid content length",
                "CELESTRAK_RESPONSE_INVALID",
            )
        if int(lengths[0]) > maximum_response_bytes:
            raise CelesTrakResponseError(
                "CelesTrak response exceeded the configured limit",
                "CELESTRAK_RESPONSE_TOO_LARGE",
            )


def _retry_after_seconds(headers: httpx2.Headers) -> float | None:
    values = headers.get_list("retry-after")
    if len(values) != 1:
        return None
    try:
        value = float(values[0])
    except ValueError:
        return None
    if not math.isfinite(value) or value < 0:
        return None
    return min(value, 2.0)


def _reject_duplicate_members(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("JSON object contains duplicate members")
        result[key] = value
    return result


def _reject_nonfinite_constant(value: str) -> None:
    raise ValueError(f"Non-finite JSON number is not allowed: {value}")


def _required_string(
    record: Mapping[str, Any],
    key: str,
    *,
    maximum_length: int,
) -> str:
    value = record.get(key)
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > maximum_length
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)
    ):
        raise CelesTrakResponseError(
            f"CelesTrak field {key} is invalid",
            "CELESTRAK_RESPONSE_INVALID",
        )
    return value


def _optional_string(
    record: Mapping[str, Any],
    key: str,
    *,
    maximum_length: int,
) -> str | None:
    value = record.get(key)
    if value is None or value == "":
        return None
    return _required_string(record, key, maximum_length=maximum_length)


def _required_number(
    record: Mapping[str, Any],
    key: str,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float:
    value = record.get(key)
    if type(value) not in {int, float}:
        raise CelesTrakResponseError(
            f"CelesTrak field {key} is invalid",
            "CELESTRAK_RESPONSE_INVALID",
        )
    normalized = float(value)
    if (
        not math.isfinite(normalized)
        or (minimum is not None and normalized <= minimum and key == "MEAN_MOTION")
        or (minimum is not None and normalized < minimum and key != "MEAN_MOTION")
        or (maximum is not None and normalized > maximum)
    ):
        raise CelesTrakResponseError(
            f"CelesTrak field {key} is outside its supported range",
            "CELESTRAK_RESPONSE_INVALID",
        )
    return normalized


def _optional_number(record: Mapping[str, Any], key: str) -> float | None:
    if record.get(key) is None:
        return None
    return _required_number(record, key)


def _required_integer(
    record: Mapping[str, Any],
    key: str,
    *,
    minimum: int,
) -> int:
    value = record.get(key)
    if type(value) is not int or value < minimum:
        raise CelesTrakResponseError(
            f"CelesTrak field {key} is invalid",
            "CELESTRAK_RESPONSE_INVALID",
        )
    return value


def _optional_integer(
    record: Mapping[str, Any],
    key: str,
    *,
    minimum: int,
) -> int | None:
    if record.get(key) is None:
        return None
    return _required_integer(record, key, minimum=minimum)


def _required_epoch(record: Mapping[str, Any], key: str) -> datetime:
    value = _required_string(record, key, maximum_length=64)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise CelesTrakResponseError(
            "CelesTrak epoch is invalid",
            "CELESTRAK_RESPONSE_INVALID",
        ) from error
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("CelesTrak fetch clock must be timezone-aware")
    return value.astimezone(UTC)
