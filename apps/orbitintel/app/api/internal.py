"""Authenticated private fulfillment protocol consumed only by MeterGate."""

from __future__ import annotations

import contextlib
import json
from hmac import compare_digest
from typing import Annotated, cast

from fastapi import APIRouter, Depends, Request, Response, status
from pydantic import ValidationError

from app.core.config import Settings
from app.errors import (
    FaultInjectedError,
    IdempotencyIntegrityError,
    InputBindingError,
    InternalAuthenticationError,
    OrbitIntelError,
    RequestBodyError,
)
from app.hashing import canonical_json_bytes, canonical_json_hash, fulfillment_request_hash
from app.idempotency import (
    ExecutionClaim,
    RedisExecutionIdempotency,
    ReplayedExecution,
)
from app.schemas import (
    DetailedOrbitalAnalysisResult,
    ErrorResponse,
    FulfillmentExecutionId,
    FulfillmentRequest,
    FulfillmentResponse,
    OrbitIntelResult,
    ServiceSlug,
)
from app.services import OrbitIntelService

router = APIRouter(prefix="/internal/v1", tags=["private-fulfillment"])


def get_settings(request: Request) -> Settings:
    settings = getattr(request.app.state, "settings", None)
    if not isinstance(settings, Settings):
        raise RuntimeError("OrbitIntel settings are not configured")
    return settings


def get_orbitintel_service(request: Request) -> OrbitIntelService:
    service = getattr(request.app.state, "orbitintel_service", None)
    if not isinstance(service, OrbitIntelService):
        raise RuntimeError("OrbitIntel service is not configured")
    return service


def get_idempotency(request: Request) -> RedisExecutionIdempotency:
    idempotency = getattr(request.app.state, "idempotency", None)
    if not isinstance(idempotency, RedisExecutionIdempotency):
        raise RuntimeError("OrbitIntel idempotency storage is not configured")
    return idempotency


SettingsDependency = Annotated[Settings, Depends(get_settings)]
ServiceDependency = Annotated[OrbitIntelService, Depends(get_orbitintel_service)]
IdempotencyDependency = Annotated[RedisExecutionIdempotency, Depends(get_idempotency)]


def require_internal_authentication(
    request: Request,
    settings: SettingsDependency,
) -> None:
    values = [
        value for key, value in request.scope.get("headers", ()) if key.lower() == b"authorization"
    ]
    provided = ""
    if len(values) == 1:
        try:
            header = values[0].decode("latin-1")
        except UnicodeDecodeError:
            header = ""
        if header.startswith("Bearer ") and header.count(" ") == 1:
            provided = header[7:]
    expected = settings.orbitintel_shared_secret.get_secret_value()
    if not provided or len(provided) != len(expected) or not compare_digest(provided, expected):
        raise InternalAuthenticationError(
            "Valid OrbitIntel internal authentication is required",
            "ORBITINTEL_AUTH_INVALID",
        )


InternalAuthenticationDependency = Annotated[
    None,
    Depends(require_internal_authentication),
]


@router.post(
    "/fulfillments/{fulfillment_execution_id}",
    response_model=FulfillmentResponse,
    responses={
        401: {"model": ErrorResponse},
        413: {"model": ErrorResponse},
        404: {"model": ErrorResponse},
        409: {"model": ErrorResponse},
        422: {"model": ErrorResponse},
        502: {"model": ErrorResponse},
        503: {"model": ErrorResponse},
    },
)
async def execute_fulfillment(
    fulfillment_execution_id: FulfillmentExecutionId,
    request: Request,
    authenticated: InternalAuthenticationDependency,
    settings: SettingsDependency,
    service: ServiceDependency,
    idempotency: IdempotencyDependency,
) -> Response:
    del authenticated
    payload = await _read_bounded_fulfillment_request(
        request,
        maximum_bytes=settings.orbitintel_request_max_bytes,
    )
    _apply_fault_mode(settings)
    input_value = payload.input.model_dump(mode="json")
    actual_input_hash = canonical_json_hash(input_value)
    if not compare_digest(actual_input_hash, payload.input_hash):
        raise InputBindingError(
            "OrbitIntel input does not match its canonical input hash",
            "ORBITINTEL_INPUT_HASH_MISMATCH",
        )
    request_hash = fulfillment_request_hash(
        fulfillment_execution_id=fulfillment_execution_id,
        service_id=payload.service_id,
        service_slug=payload.service_slug,
        input_value=input_value,
        input_hash=payload.input_hash,
    )
    claim_or_replay = await idempotency.claim(
        fulfillment_execution_id,
        request_hash,
    )
    if isinstance(claim_or_replay, ReplayedExecution):
        _verify_replay(
            claim_or_replay.response_body,
            fulfillment_execution_id=fulfillment_execution_id,
            service_id=payload.service_id,
            service_slug=payload.service_slug,
            input_hash=payload.input_hash,
            norad_id=payload.input.norad_id,
        )
        return Response(
            content=claim_or_replay.response_body,
            status_code=status.HTTP_200_OK,
            media_type="application/json",
            headers={"X-OrbitIntel-Idempotent-Replay": "true"},
        )

    claim = cast(ExecutionClaim, claim_or_replay)
    try:
        result = await service.execute(payload.service_slug, payload.input)
    except OrbitIntelError:
        with contextlib.suppress(BaseException):
            await idempotency.abandon(claim, retry_safe=True)
        raise
    except BaseException:
        with contextlib.suppress(BaseException):
            await idempotency.abandon(claim, retry_safe=False)
        raise
    try:
        response_model = FulfillmentResponse(
            fulfillment_execution_id=fulfillment_execution_id,
            service_id=payload.service_id,
            input_hash=payload.input_hash,
            result=result,
        )
        response_body = canonical_json_bytes(
            response_model.model_dump(mode="json", exclude_none=True)
        )
        await idempotency.complete(claim, response_body)
    except BaseException:
        with contextlib.suppress(BaseException):
            await idempotency.abandon(claim, retry_safe=False)
        raise
    return Response(
        content=response_body,
        status_code=status.HTTP_200_OK,
        media_type="application/json",
        headers={"X-OrbitIntel-Idempotent-Replay": "false"},
    )


def _single_content_length(request: Request) -> int | None:
    values = [
        value for key, value in request.scope.get("headers", ()) if key.lower() == b"content-length"
    ]
    if not values:
        return None
    if len(values) != 1:
        raise RequestBodyError(
            "OrbitIntel Content-Length must be supplied at most once",
            "ORBITINTEL_REQUEST_INVALID",
        )
    try:
        raw_value = values[0].decode("ascii")
    except UnicodeDecodeError as error:
        raise RequestBodyError(
            "OrbitIntel Content-Length is invalid",
            "ORBITINTEL_REQUEST_INVALID",
        ) from error
    if not raw_value.isascii() or not raw_value.isdecimal():
        raise RequestBodyError(
            "OrbitIntel Content-Length is invalid",
            "ORBITINTEL_REQUEST_INVALID",
        )
    return int(raw_value)


async def _read_bounded_fulfillment_request(
    request: Request,
    *,
    maximum_bytes: int,
) -> FulfillmentRequest:
    declared_length = _single_content_length(request)
    if declared_length is not None and declared_length > maximum_bytes:
        raise RequestBodyError(
            "OrbitIntel request body exceeds the configured limit",
            "ORBITINTEL_REQUEST_BODY_TOO_LARGE",
        )

    body = bytearray()
    async for chunk in request.stream():
        if len(chunk) > maximum_bytes - len(body):
            raise RequestBodyError(
                "OrbitIntel request body exceeds the configured limit",
                "ORBITINTEL_REQUEST_BODY_TOO_LARGE",
            )
        body.extend(chunk)
    try:
        return FulfillmentRequest.model_validate_json(bytes(body))
    except (json.JSONDecodeError, ValidationError, ValueError, TypeError) as error:
        raise RequestBodyError(
            "OrbitIntel request validation failed",
            "ORBITINTEL_REQUEST_INVALID",
        ) from error


def _apply_fault_mode(settings: Settings) -> None:
    if settings.orbitintel_dev_fault_mode == "retryable":
        raise FaultInjectedError(
            "OrbitIntel development fault injection forced a retryable failure",
            "ORBITINTEL_FAULT_RETRYABLE",
            retryable=True,
        )
    if settings.orbitintel_dev_fault_mode == "permanent":
        raise FaultInjectedError(
            "OrbitIntel development fault injection forced a permanent failure",
            "ORBITINTEL_FAULT_PERMANENT",
            retryable=False,
        )


def _verify_replay(
    response_body: bytes,
    *,
    fulfillment_execution_id: str,
    service_id: str,
    service_slug: ServiceSlug,
    input_hash: str,
    norad_id: int,
) -> None:
    try:
        response = FulfillmentResponse.model_validate_json(response_body)
    except (ValidationError, ValueError, TypeError) as error:
        raise IdempotencyIntegrityError(
            "Persisted OrbitIntel response failed validation",
            "ORBITINTEL_IDEMPOTENCY_INTEGRITY_FAILED",
        ) from error
    expected_result_type = {
        "satellite-status-lookup": "satellite_status",
        "orbital-risk-report": "orbital_risk_report",
        "detailed-orbital-analysis": "detailed_orbital_analysis",
    }[service_slug]
    if (
        response.fulfillment_execution_id != fulfillment_execution_id
        or response.service_id != service_id
        or not compare_digest(response.input_hash, input_hash)
        or response.result.result_type != expected_result_type
        or _result_norad_id(response.result) != norad_id
    ):
        raise IdempotencyIntegrityError(
            "Persisted OrbitIntel response has a different execution binding",
            "ORBITINTEL_IDEMPOTENCY_INTEGRITY_FAILED",
        )


def _result_norad_id(result: OrbitIntelResult) -> int:
    if isinstance(result, DetailedOrbitalAnalysisResult):
        return result.source.norad_id
    return result.norad_id
