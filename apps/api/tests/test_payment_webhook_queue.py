from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from redis.exceptions import RedisError, ResponseError

from app.cache.payment_webhooks import (
    RedisPaymentWebhookQueue,
    VerifiedWebhookPayload,
    WebhookQueueDurabilityError,
    WebhookQueueMessageError,
    WebhookQueueUnavailableError,
)
from app.domain.hashing import canonical_utc_datetime

NOW = datetime(2026, 8, 25, 12, 34, 56, 123456, tzinfo=UTC)
EVENT_ID = "delivery/2026:opaque-ABC_123"
STREAM_NAME = "metergate:razorpay:webhooks:v1"


class FakePipeline:
    def __init__(self, redis: FakeRedis) -> None:
        self.redis = redis
        self.commands: list[tuple[Any, ...]] = []

    def xadd(self, name: str, fields: dict[str, object]) -> FakePipeline:
        self.commands.append(("xadd", name, fields))
        return self

    def waitaof(self, local: int, replicas: int, timeout_ms: int) -> FakePipeline:
        self.commands.append(("waitaof", local, replicas, timeout_ms))
        return self

    def xack(self, name: str, groupname: str, *ids: str) -> FakePipeline:
        self.commands.append(("xack", name, groupname, ids))
        return self

    def xdel(self, name: str, *ids: str) -> FakePipeline:
        self.commands.append(("xdel", name, ids))
        return self

    async def execute(self, raise_on_error: bool = True) -> list[object]:
        self.redis.calls.append(("execute", tuple(self.commands)))
        if self.commands and self.commands[0][0] == "xadd":
            return [self.redis.xadd_result, self.redis.waitaof_result]
        result = self.redis.pipeline_result
        if isinstance(result, Exception):
            raise result
        return result


class FakeRedis:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.xadd_result: object = b"100-0"
        self.waitaof_result: object = [1, 0]
        self.group_error: Exception | None = None
        self.read_result: object = []
        self.claim_result: object = [b"0-0", [], []]
        self.pipeline_result: list[int] | Exception = [1, 1]

    async def xgroup_create(
        self,
        name: str,
        groupname: str,
        id: str,
        *,
        mkstream: bool,
    ) -> object:
        self.calls.append(("xgroup_create", name, groupname, id, mkstream))
        if self.group_error is not None:
            raise self.group_error
        return True

    async def xreadgroup(
        self,
        groupname: str,
        consumername: str,
        streams: dict[str, str],
        *,
        count: int,
        block: int,
        noack: bool,
    ) -> object:
        self.calls.append(("xreadgroup", groupname, consumername, streams, count, block, noack))
        return self.read_result

    async def xautoclaim(
        self,
        name: str,
        groupname: str,
        consumername: str,
        min_idle_time: int,
        start_id: str,
        *,
        count: int,
    ) -> object:
        self.calls.append(
            (
                "xautoclaim",
                name,
                groupname,
                consumername,
                min_idle_time,
                start_id,
                count,
            )
        )
        return self.claim_result

    def pipeline(self, *, transaction: bool) -> FakePipeline:
        self.calls.append(("pipeline", transaction))
        return FakePipeline(self)


def payload() -> VerifiedWebhookPayload:
    return VerifiedWebhookPayload(
        provider_event_id=EVENT_ID,
        received_at=NOW,
        raw_body=b'{"event":"payment.captured"}',
    )


def encoded_message(stream_id: bytes = b"100-0") -> tuple[bytes, dict[bytes, bytes]]:
    return (
        stream_id,
        {
            b"version": b"1",
            b"provider_event_id": EVENT_ID.encode(),
            b"received_at": canonical_utc_datetime(NOW).encode(),
            b"raw_body": b'{"event":"payment.captured"}',
        },
    )


@pytest.mark.asyncio
async def test_enqueue_appends_exact_bytes_then_requires_local_aof_confirmation() -> None:
    redis = FakeRedis()
    queue = RedisPaymentWebhookQueue(redis)

    stream_id = await queue.enqueue(payload())

    assert stream_id == "100-0"
    assert redis.calls == [
        ("pipeline", False),
        (
            "execute",
            (
                (
                    "xadd",
                    STREAM_NAME,
                    {
                        "version": "1",
                        "provider_event_id": EVENT_ID,
                        "received_at": canonical_utc_datetime(NOW),
                        "raw_body": b'{"event":"payment.captured"}',
                    },
                ),
                ("waitaof", 1, 0, 3000),
            ),
        ),
    ]


@pytest.mark.asyncio
async def test_enqueue_distinguishes_append_and_ambiguous_durability_failures() -> None:
    redis = FakeRedis()
    redis.xadd_result = RedisError("redis secret")
    with pytest.raises(WebhookQueueUnavailableError, match="enqueue failed"):
        await RedisPaymentWebhookQueue(redis).enqueue(payload())

    redis = FakeRedis()
    redis.waitaof_result = [0, 0]
    with pytest.raises(WebhookQueueDurabilityError, match="not confirmed durable"):
        await RedisPaymentWebhookQueue(redis).enqueue(payload())
    assert [call[0] for call in redis.calls] == ["pipeline", "execute"]


@pytest.mark.asyncio
async def test_consumer_group_read_reclaim_and_transactional_ack_delete() -> None:
    redis = FakeRedis()
    redis.group_error = ResponseError("BUSYGROUP Consumer Group name already exists")
    redis.read_result = [[STREAM_NAME.encode(), [encoded_message()]]]
    redis.claim_result = [b"0-0", [encoded_message(b"101-0")], []]
    queue = RedisPaymentWebhookQueue(redis)

    await queue.initialize()
    new_messages = await queue.read_new("worker-1", count=5, block_ms=250)
    claimed = await queue.claim_stale(
        "worker-1",
        minimum_idle_ms=30_000,
        count=5,
    )
    acknowledged = await queue.acknowledge_and_delete(["100-0"])

    assert new_messages[0].payload == payload()
    assert claimed.next_start_id == "0-0"
    assert claimed.messages[0].stream_id == "101-0"
    assert acknowledged == 1
    assert redis.calls[-1] == (
        "execute",
        (
            (
                "xack",
                STREAM_NAME,
                "metergate-payment-webhooks-v1",
                ("100-0",),
            ),
            ("xdel", STREAM_NAME, ("100-0",)),
        ),
    )


@pytest.mark.asyncio
async def test_consumer_rejects_malformed_or_text_decoded_raw_body() -> None:
    redis = FakeRedis()
    message_id, fields = encoded_message()
    fields[b"raw_body"] = b"valid bytes"
    fields["raw_body"] = "decoded text"  # type: ignore[index]
    redis.read_result = [[STREAM_NAME.encode(), [(message_id, fields)]]]

    with pytest.raises(WebhookQueueMessageError, match="invalid"):
        await RedisPaymentWebhookQueue(redis).read_new("worker-1")


def test_provider_event_id_is_opaque_bounded_printable_ascii() -> None:
    assert payload().provider_event_id == EVENT_ID
    for invalid in ("", "event with space", "event\nline", "é", "x" * 129):
        with pytest.raises(ValueError, match="event ID"):
            VerifiedWebhookPayload(provider_event_id=invalid, received_at=NOW, raw_body=b"{}")
