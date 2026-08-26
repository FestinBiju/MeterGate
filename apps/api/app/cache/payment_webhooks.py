"""Durable Redis Stream transport for already-authenticated payment webhooks."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable

from redis.exceptions import RedisError, ResponseError

from app.domain.hashing import canonical_utc_datetime

PAYMENT_WEBHOOK_MESSAGE_VERSION = "1"
DEFAULT_PAYMENT_WEBHOOK_STREAM = "metergate:razorpay:webhooks:v1"
DEFAULT_PAYMENT_WEBHOOK_CONSUMER_GROUP = "metergate-payment-webhooks-v1"
MAXIMUM_WEBHOOK_BODY_BYTES = 1_048_576

_STREAM_ID_PATTERN = re.compile(r"^[0-9]+-[0-9]+$")
_REDIS_NAME_PATTERN = re.compile(r"^[A-Za-z0-9:_-]{1,128}$")


class WebhookQueueError(RuntimeError):
    """Base class for sanitized payment-webhook transport failures."""


class WebhookQueueUnavailableError(WebhookQueueError):
    """Redis was unavailable while operating on the webhook stream."""


class WebhookQueueDurabilityError(WebhookQueueError):
    """Redis did not confirm local AOF persistence for an enqueued message."""


class WebhookQueueMessageError(WebhookQueueError):
    """A stored stream message failed strict validation."""


@dataclass(frozen=True, slots=True)
class VerifiedWebhookPayload:
    """Byte-exact webhook data admitted only after HTTP-layer authentication."""

    provider_event_id: str
    received_at: datetime
    raw_body: bytes

    def __post_init__(self) -> None:
        if not _valid_provider_event_id(self.provider_event_id):
            raise ValueError("Provider event ID is invalid")
        if (
            not isinstance(self.received_at, datetime)
            or self.received_at.tzinfo is None
            or self.received_at.utcoffset() is None
        ):
            raise ValueError("Webhook receipt timestamp must be timezone-aware")
        if (
            type(self.raw_body) is not bytes
            or not 1 <= len(self.raw_body) <= MAXIMUM_WEBHOOK_BODY_BYTES
        ):
            raise ValueError("Webhook body must be non-empty bounded raw bytes")
        object.__setattr__(self, "received_at", self.received_at.astimezone(UTC))


def _valid_provider_event_id(value: object) -> bool:
    """Accept Razorpay's documented opaque ID without assuming an undocumented prefix."""
    return (
        isinstance(value, str)
        and 1 <= len(value) <= 128
        and all(0x21 <= ord(character) <= 0x7E for character in value)
    )


@dataclass(frozen=True, slots=True)
class QueuedWebhookMessage:
    stream_id: str
    payload: VerifiedWebhookPayload

    def __post_init__(self) -> None:
        if (
            not isinstance(self.stream_id, str)
            or _STREAM_ID_PATTERN.fullmatch(self.stream_id) is None
        ):
            raise ValueError("Redis stream ID is invalid")


@dataclass(frozen=True, slots=True)
class ClaimedWebhookBatch:
    next_start_id: str
    messages: tuple[QueuedWebhookMessage, ...]

    def __post_init__(self) -> None:
        if (
            not isinstance(self.next_start_id, str)
            or _STREAM_ID_PATTERN.fullmatch(self.next_start_id) is None
        ):
            raise ValueError("Redis claim cursor is invalid")


@runtime_checkable
class WebhookQueuePublisher(Protocol):
    async def enqueue(self, payload: VerifiedWebhookPayload) -> str: ...


@runtime_checkable
class WebhookQueueConsumer(Protocol):
    async def initialize(self) -> None: ...

    async def read_new(
        self,
        consumer_name: str,
        *,
        count: int = 10,
        block_ms: int = 5_000,
    ) -> tuple[QueuedWebhookMessage, ...]: ...

    async def claim_stale(
        self,
        consumer_name: str,
        *,
        minimum_idle_ms: int,
        start_id: str = "0-0",
        count: int = 10,
    ) -> ClaimedWebhookBatch: ...

    async def acknowledge_and_delete(self, message_ids: Sequence[str]) -> int: ...


class WebhookQueue(WebhookQueuePublisher, WebhookQueueConsumer, Protocol):
    """Combined publisher/consumer interface used by small deployments."""


class _RedisPipeline(Protocol):
    def xadd(self, name: str, fields: Mapping[str, object]) -> _RedisPipeline: ...

    def waitaof(
        self,
        num_local: int,
        num_replicas: int,
        timeout_ms: int,
    ) -> _RedisPipeline: ...

    def xack(self, name: str, groupname: str, *ids: str) -> _RedisPipeline: ...

    def xdel(self, name: str, *ids: str) -> _RedisPipeline: ...

    async def execute(self, raise_on_error: bool = True) -> Sequence[object]: ...


class RedisPaymentWebhookClient(Protocol):
    async def xgroup_create(
        self,
        name: str,
        groupname: str,
        id: str,
        *,
        mkstream: bool,
    ) -> object: ...

    async def xreadgroup(
        self,
        groupname: str,
        consumername: str,
        streams: Mapping[str, str],
        *,
        count: int,
        block: int,
        noack: bool,
    ) -> object: ...

    async def xautoclaim(
        self,
        name: str,
        groupname: str,
        consumername: str,
        min_idle_time: int,
        start_id: str,
        *,
        count: int,
    ) -> object: ...

    def pipeline(self, *, transaction: bool) -> _RedisPipeline: ...


class RedisPaymentWebhookQueue:
    """Redis Stream queue with AOF confirmation and consumer-group recovery."""

    def __init__(
        self,
        client: RedisPaymentWebhookClient,
        *,
        stream_name: str = DEFAULT_PAYMENT_WEBHOOK_STREAM,
        consumer_group: str = DEFAULT_PAYMENT_WEBHOOK_CONSUMER_GROUP,
        durability_timeout_ms: int = 3_000,
    ) -> None:
        if not isinstance(stream_name, str) or _REDIS_NAME_PATTERN.fullmatch(stream_name) is None:
            raise ValueError("Webhook stream name is invalid")
        if (
            not isinstance(consumer_group, str)
            or _REDIS_NAME_PATTERN.fullmatch(consumer_group) is None
        ):
            raise ValueError("Webhook consumer group is invalid")
        if type(durability_timeout_ms) is not int or not 1 <= durability_timeout_ms <= 10_000:
            raise ValueError("Webhook durability timeout must be between one and 10000 ms")
        self._client = client
        self._stream_name = stream_name
        self._consumer_group = consumer_group
        self._durability_timeout_ms = durability_timeout_ms

    async def enqueue(self, payload: VerifiedWebhookPayload) -> str:
        if not isinstance(payload, VerifiedWebhookPayload):
            raise ValueError("A validated webhook payload is required")
        fields: dict[str, object] = {
            "version": PAYMENT_WEBHOOK_MESSAGE_VERSION,
            "provider_event_id": payload.provider_event_id,
            "received_at": canonical_utc_datetime(payload.received_at),
            "raw_body": payload.raw_body,
        }
        try:
            # WAITAOF only covers prior writes on the same Redis connection. A
            # non-transactional pipeline guarantees XADD and WAITAOF share one.
            pipeline = self._client.pipeline(transaction=False)
            pipeline.xadd(self._stream_name, fields)
            pipeline.waitaof(1, 0, self._durability_timeout_ms)
            result = await pipeline.execute(raise_on_error=False)
        except (RedisError, OSError) as error:
            raise WebhookQueueDurabilityError(
                "Payment webhook AOF confirmation failed",
            ) from error
        if not isinstance(result, (list, tuple)) or len(result) != 2:
            raise WebhookQueueDurabilityError("Payment webhook durability response was invalid")
        raw_stream_id, persistence = result
        if isinstance(raw_stream_id, (RedisError, OSError)):
            raise WebhookQueueUnavailableError("Payment webhook enqueue failed") from raw_stream_id
        if isinstance(persistence, (RedisError, OSError)):
            raise WebhookQueueDurabilityError(
                "Payment webhook AOF confirmation failed",
            ) from persistence
        try:
            stream_id = self._decode_stream_id(raw_stream_id)
        except (TypeError, ValueError) as error:
            raise WebhookQueueUnavailableError("Payment webhook enqueue failed") from error
        if not self._has_local_aof_confirmation(persistence):
            raise WebhookQueueDurabilityError("Payment webhook was not confirmed durable")
        return stream_id

    async def initialize(self) -> None:
        try:
            await self._client.xgroup_create(
                self._stream_name,
                self._consumer_group,
                "0-0",
                mkstream=True,
            )
        except ResponseError as error:
            if "BUSYGROUP" not in str(error):
                raise WebhookQueueUnavailableError(
                    "Payment webhook consumer group initialization failed",
                ) from error
        except (RedisError, OSError) as error:
            raise WebhookQueueUnavailableError(
                "Payment webhook consumer group initialization failed",
            ) from error

    async def read_new(
        self,
        consumer_name: str,
        *,
        count: int = 10,
        block_ms: int = 5_000,
    ) -> tuple[QueuedWebhookMessage, ...]:
        self._validate_consumer_name(consumer_name)
        self._validate_count(count)
        if type(block_ms) is not int or not 0 <= block_ms <= 60_000:
            raise ValueError("Webhook stream block time must be between zero and 60000 ms")
        try:
            raw = await self._client.xreadgroup(
                self._consumer_group,
                consumer_name,
                {self._stream_name: ">"},
                count=count,
                block=block_ms,
                noack=False,
            )
        except (RedisError, OSError) as error:
            raise WebhookQueueUnavailableError("Payment webhook stream read failed") from error
        return self._parse_read_result(raw)

    async def claim_stale(
        self,
        consumer_name: str,
        *,
        minimum_idle_ms: int,
        start_id: str = "0-0",
        count: int = 10,
    ) -> ClaimedWebhookBatch:
        self._validate_consumer_name(consumer_name)
        self._validate_count(count)
        if type(minimum_idle_ms) is not int or not 1 <= minimum_idle_ms <= 86_400_000:
            raise ValueError("Webhook claim idle time must be positive and bounded")
        self._validate_stream_id(start_id)
        try:
            raw = await self._client.xautoclaim(
                self._stream_name,
                self._consumer_group,
                consumer_name,
                minimum_idle_ms,
                start_id,
                count=count,
            )
        except (RedisError, OSError) as error:
            raise WebhookQueueUnavailableError("Payment webhook reclaim failed") from error
        return self._parse_claim_result(raw)

    async def acknowledge_and_delete(self, message_ids: Sequence[str]) -> int:
        if isinstance(message_ids, (str, bytes)) or not 1 <= len(message_ids) <= 100:
            raise ValueError("One to 100 webhook message IDs are required")
        normalized_ids = tuple(message_ids)
        if len(set(normalized_ids)) != len(normalized_ids):
            raise ValueError("Webhook message IDs must be unique")
        for message_id in normalized_ids:
            self._validate_stream_id(message_id)
        try:
            pipeline = self._client.pipeline(transaction=True)
            pipeline.xack(self._stream_name, self._consumer_group, *normalized_ids)
            pipeline.xdel(self._stream_name, *normalized_ids)
            result = await pipeline.execute()
        except (RedisError, OSError) as error:
            raise WebhookQueueUnavailableError(
                "Payment webhook acknowledgement failed",
            ) from error
        if (
            not isinstance(result, (list, tuple))
            or len(result) != 2
            or type(result[0]) is not int
            or type(result[1]) is not int
            or result[0] < 0
            or result[1] < 0
        ):
            raise WebhookQueueUnavailableError("Payment webhook acknowledgement was invalid")
        return result[0]

    def _parse_read_result(self, raw: object) -> tuple[QueuedWebhookMessage, ...]:
        if raw in (None, []):
            return ()
        if not isinstance(raw, (list, tuple)) or len(raw) != 1:
            raise WebhookQueueMessageError("Payment webhook stream response was invalid")
        stream = raw[0]
        if not isinstance(stream, (list, tuple)) or len(stream) != 2:
            raise WebhookQueueMessageError("Payment webhook stream response was invalid")
        if self._decode_text(stream[0], field="stream name") != self._stream_name:
            raise WebhookQueueMessageError("Payment webhook stream name did not match")
        return self._parse_messages(stream[1])

    def _parse_claim_result(self, raw: object) -> ClaimedWebhookBatch:
        if not isinstance(raw, (list, tuple)) or len(raw) not in {2, 3}:
            raise WebhookQueueMessageError("Payment webhook claim response was invalid")
        try:
            next_start_id = self._decode_stream_id(raw[0])
            messages = self._parse_messages(raw[1])
        except (TypeError, ValueError) as error:
            raise WebhookQueueMessageError(
                "Payment webhook claim response was invalid",
            ) from error
        if len(raw) == 3 and not isinstance(raw[2], (list, tuple)):
            raise WebhookQueueMessageError("Payment webhook deleted-ID response was invalid")
        return ClaimedWebhookBatch(next_start_id=next_start_id, messages=messages)

    def _parse_messages(self, raw: object) -> tuple[QueuedWebhookMessage, ...]:
        if not isinstance(raw, (list, tuple)) or len(raw) > 100:
            raise WebhookQueueMessageError("Payment webhook messages were invalid")
        messages: list[QueuedWebhookMessage] = []
        try:
            for entry in raw:
                if not isinstance(entry, (list, tuple)) or len(entry) != 2:
                    raise TypeError("invalid stream entry")
                stream_id = self._decode_stream_id(entry[0])
                fields = self._normalize_fields(entry[1])
                if fields["version"] != PAYMENT_WEBHOOK_MESSAGE_VERSION:
                    raise ValueError("unsupported message version")
                received_at_text = fields["received_at"]
                received_at = datetime.fromisoformat(received_at_text.replace("Z", "+00:00"))
                if canonical_utc_datetime(received_at) != received_at_text:
                    raise ValueError("non-canonical receipt timestamp")
                raw_body = fields["raw_body"]
                if type(raw_body) is not bytes:
                    raise TypeError("webhook body is not bytes")
                payload = VerifiedWebhookPayload(
                    provider_event_id=fields["provider_event_id"],
                    received_at=received_at,
                    raw_body=raw_body,
                )
                messages.append(QueuedWebhookMessage(stream_id=stream_id, payload=payload))
        except (KeyError, TypeError, ValueError, OverflowError) as error:
            raise WebhookQueueMessageError("Payment webhook message was invalid") from error
        return tuple(messages)

    @classmethod
    def _normalize_fields(cls, raw: object) -> dict[str, Any]:
        if not isinstance(raw, Mapping):
            raise TypeError("stream fields are not a mapping")
        normalized: dict[str, Any] = {}
        for raw_key, raw_value in raw.items():
            key = cls._decode_text(raw_key, field="field name")
            if key in normalized:
                raise ValueError("duplicate stream field")
            if key == "raw_body":
                normalized[key] = raw_value
            else:
                normalized[key] = cls._decode_text(raw_value, field=key)
        expected = {"version", "provider_event_id", "received_at", "raw_body"}
        if set(normalized) != expected:
            raise ValueError("unexpected stream fields")
        return normalized

    @staticmethod
    def _has_local_aof_confirmation(value: object) -> bool:
        return (
            isinstance(value, (list, tuple))
            and len(value) == 2
            and type(value[0]) is int
            and type(value[1]) is int
            and value[0] >= 1
            and value[1] >= 0
        )

    @classmethod
    def _decode_stream_id(cls, value: object) -> str:
        result = cls._decode_text(value, field="stream ID")
        cls._validate_stream_id(result)
        return result

    @staticmethod
    def _decode_text(value: object, *, field: str) -> str:
        if isinstance(value, bytes):
            try:
                value = value.decode("ascii")
            except UnicodeDecodeError as error:
                raise ValueError(f"Redis {field} was not ASCII") from error
        if not isinstance(value, str):
            raise TypeError(f"Redis {field} was not text")
        return value

    @staticmethod
    def _validate_stream_id(stream_id: str) -> None:
        if not isinstance(stream_id, str) or _STREAM_ID_PATTERN.fullmatch(stream_id) is None:
            raise ValueError("Redis stream ID is invalid")

    @staticmethod
    def _validate_consumer_name(consumer_name: str) -> None:
        if (
            not isinstance(consumer_name, str)
            or _REDIS_NAME_PATTERN.fullmatch(consumer_name) is None
        ):
            raise ValueError("Webhook consumer name is invalid")

    @staticmethod
    def _validate_count(count: int) -> None:
        if type(count) is not int or not 1 <= count <= 100:
            raise ValueError("Webhook batch count must be between one and 100")
