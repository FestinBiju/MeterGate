import asyncio
import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

import httpx2
import pytest

from app.celestrak import CELESTRAK_GP_URL, CelesTrakClient, parse_gp_response
from app.errors import (
    CelesTrakNotFoundError,
    CelesTrakResponseError,
    CelesTrakUnavailableError,
)

FETCHED_AT = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)


def gp_record(**changes: object) -> dict[str, object]:
    record: dict[str, object] = {
        "OBJECT_NAME": "ISS (ZARYA)",
        "OBJECT_ID": "1998-067A",
        "EPOCH": "2026-08-26T06:00:00.000000",
        "MEAN_MOTION": 15.5,
        "ECCENTRICITY": 0.0005,
        "INCLINATION": 51.64,
        "RA_OF_ASC_NODE": 122.5,
        "ARG_OF_PERICENTER": 73.2,
        "MEAN_ANOMALY": 10.4,
        "CLASSIFICATION_TYPE": "U",
        "NORAD_CAT_ID": 25544,
        "ELEMENT_SET_NO": 999,
        "REV_AT_EPOCH": 52345,
        "BSTAR": 0.000123,
        "MEAN_MOTION_DOT": 0.0001,
        "MEAN_MOTION_DDOT": 0.0,
    }
    record.update(changes)
    return record


def make_client(
    handler: Callable[[httpx2.Request], httpx2.Response | Awaitable[httpx2.Response]],
    *,
    attempts: int = 1,
    maximum_bytes: int = 65_536,
    request_timeout_seconds: float = 1.0,
    delays: list[float] | None = None,
) -> tuple[CelesTrakClient, httpx2.AsyncClient]:
    async def sleep(delay: float) -> None:
        if delays is not None:
            delays.append(delay)

    http_client = httpx2.AsyncClient(
        transport=httpx2.MockTransport(handler),
        headers={
            "Accept": "application/json",
            "User-Agent": "OrbitIntel tests (descriptive GP client)",
        },
        follow_redirects=False,
    )
    return (
        CelesTrakClient(
            http_client,
            maximum_response_bytes=maximum_bytes,
            maximum_attempts=attempts,
            retry_base_delay_seconds=0.1,
            request_timeout_seconds=request_timeout_seconds,
            clock=lambda: FETCHED_AT,
            sleep=sleep,
        ),
        http_client,
    )


@pytest.mark.asyncio
async def test_hard_request_deadline_bounds_the_entire_upstream_operation() -> None:
    async def handler(_request: httpx2.Request) -> httpx2.Response:
        await asyncio.sleep(0.05)
        return httpx2.Response(
            200,
            headers={"Content-Type": "application/json"},
            json=[gp_record()],
        )

    client, http_client = make_client(handler, request_timeout_seconds=0.01)
    try:
        with pytest.raises(CelesTrakUnavailableError) as caught:
            await client.fetch(25544)
    finally:
        await http_client.aclose()

    assert caught.value.reason_code == "CELESTRAK_UNAVAILABLE"


@pytest.mark.asyncio
async def test_fetch_uses_exact_structured_query_and_parses_snapshot() -> None:
    seen_requests: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen_requests.append(request)
        return httpx2.Response(
            200,
            headers={"Content-Type": "application/json"},
            json=[gp_record()],
        )

    client, http_client = make_client(handler)
    try:
        snapshot = await client.fetch(25544)
    finally:
        await http_client.aclose()

    assert snapshot.norad_id == 25544
    assert snapshot.object_name == "ISS (ZARYA)"
    assert snapshot.epoch.tzinfo is UTC
    assert snapshot.fetched_at == FETCHED_AT
    assert len(seen_requests) == 1
    request = seen_requests[0]
    assert str(request.url).startswith(CELESTRAK_GP_URL)
    assert dict(request.url.params.multi_items()) == {
        "CATNR": "25544",
        "FORMAT": "JSON",
    }
    assert request.headers["user-agent"].startswith("OrbitIntel tests")


@pytest.mark.asyncio
async def test_429_and_5xx_are_retried_with_bounded_delays() -> None:
    statuses = [429, 503, 200]
    delays: list[float] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        del request
        status = statuses.pop(0)
        if status == 200:
            return httpx2.Response(
                status,
                headers={"Content-Type": "application/json"},
                json=[gp_record()],
            )
        return httpx2.Response(status, headers={"Retry-After": "0.2"})

    client, http_client = make_client(handler, attempts=3, delays=delays)
    try:
        snapshot = await client.fetch(25544)
    finally:
        await http_client.aclose()

    assert snapshot.norad_id == 25544
    assert delays == [0.2, 0.2]


@pytest.mark.asyncio
async def test_retry_exhaustion_is_sanitized() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        del request
        return httpx2.Response(503)

    client, http_client = make_client(handler, attempts=2)
    try:
        with pytest.raises(CelesTrakUnavailableError) as captured:
            await client.fetch(25544)
    finally:
        await http_client.aclose()

    assert captured.value.reason_code == "CELESTRAK_UNAVAILABLE"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [301, 400, 404])
async def test_redirects_and_4xx_are_not_retried(status: int) -> None:
    calls = 0

    def handler(request: httpx2.Request) -> httpx2.Response:
        nonlocal calls
        del request
        calls += 1
        return httpx2.Response(status, headers={"Location": "https://example.invalid"})

    client, http_client = make_client(handler, attempts=3)
    try:
        with pytest.raises(CelesTrakResponseError):
            await client.fetch(25544)
    finally:
        await http_client.aclose()

    assert calls == 1


@pytest.mark.asyncio
async def test_response_size_is_bounded_before_json_parsing() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        del request
        return httpx2.Response(
            200,
            headers={"Content-Type": "application/json"},
            content=b"[" + b" " * 2_000 + b"]",
        )

    client, http_client = make_client(handler, maximum_bytes=1_024)
    try:
        with pytest.raises(CelesTrakResponseError) as captured:
            await client.fetch(25544)
    finally:
        await http_client.aclose()

    assert captured.value.reason_code == "CELESTRAK_RESPONSE_TOO_LARGE"


@pytest.mark.asyncio
async def test_encoded_response_is_rejected_before_decompression() -> None:
    class EncodedStream(httpx2.AsyncByteStream):
        async def __aiter__(self):
            yield b"not-decoded"

    def handler(request: httpx2.Request) -> httpx2.Response:
        assert request.headers["accept-encoding"] == "identity"
        return httpx2.Response(
            200,
            headers={"Content-Type": "application/json", "Content-Encoding": "gzip"},
            stream=EncodedStream(),
        )

    client, http_client = make_client(handler)
    try:
        with pytest.raises(CelesTrakResponseError) as captured:
            await client.fetch(25544)
    finally:
        await http_client.aclose()

    assert captured.value.reason_code == "CELESTRAK_RESPONSE_INVALID"


@pytest.mark.parametrize(
    ("body", "error_type", "reason_code"),
    [
        (b"[]", CelesTrakNotFoundError, "CELESTRAK_OBJECT_NOT_FOUND"),
        (
            json.dumps([gp_record(NORAD_CAT_ID=12345)]).encode(),
            CelesTrakResponseError,
            "CELESTRAK_RESPONSE_MISMATCH",
        ),
        (
            json.dumps([gp_record(MEAN_MOTION=True)]).encode(),
            CelesTrakResponseError,
            "CELESTRAK_RESPONSE_INVALID",
        ),
        (
            b'[{"NORAD_CAT_ID":25544,"NORAD_CAT_ID":25544}]',
            CelesTrakResponseError,
            "CELESTRAK_RESPONSE_INVALID",
        ),
    ],
)
def test_parser_rejects_empty_mismatched_or_malformed_evidence(
    body: bytes,
    error_type: type[Exception],
    reason_code: str,
) -> None:
    with pytest.raises(error_type) as captured:
        parse_gp_response(body, norad_id=25544, fetched_at=FETCHED_AT)

    assert captured.value.reason_code == reason_code


@pytest.mark.asyncio
async def test_invalid_content_type_is_rejected() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        del request
        return httpx2.Response(200, headers={"Content-Type": "text/html"}, text="[]")

    client, http_client = make_client(handler)
    try:
        with pytest.raises(CelesTrakResponseError) as captured:
            await client.fetch(25544)
    finally:
        await http_client.aclose()

    assert captured.value.reason_code == "CELESTRAK_RESPONSE_INVALID"
