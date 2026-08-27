"""HTTP mappings for safe, expected domain failures."""

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse

from app.domain.exceptions import (
    ApprovalConflictError,
    ApprovalExpiredError,
    ApprovalIntegrityError,
    ApprovalNotFoundError,
    ApprovalVerificationError,
    AuthenticationConflictError,
    AuthenticationExpiredError,
    AuthenticationForbiddenError,
    AuthenticationIntegrityError,
    AuthenticationNotFoundError,
    AuthenticationUnauthorizedError,
    AuthenticationVerificationError,
    CapabilityExpiredError,
    CapabilityForbiddenError,
    CapabilityInvalidError,
    CompensationConflictError,
    CompensationForbiddenError,
    CompensationIntegrityError,
    CompensationNotFoundError,
    CompensationTimeoutError,
    CompensationUnavailableError,
    EntitlementConflictError,
    EntitlementExpiredError,
    EntitlementForbiddenError,
    EntitlementIntegrityError,
    EntitlementNotFoundError,
    EntitlementUnavailableError,
    FulfillmentConflictError,
    FulfillmentIntegrityError,
    FulfillmentPermanentError,
    FulfillmentRetryableError,
    InvalidStateTransitionError,
    McpSessionConflictError,
    McpSessionForbiddenError,
    McpSessionNotFoundError,
    McpSessionUnauthorizedError,
    PaymentConflictError,
    PaymentExpiredError,
    PaymentIntegrityError,
    PaymentNotFoundError,
    PaymentProviderError,
    PaymentTimeoutError,
    PaymentUnavailableError,
    PaymentVerificationError,
    PolicyTTLExceededError,
    QuoteConflictError,
    QuoteInputValidationError,
    ResourceNotFoundError,
    ResourceRequestError,
    SlugConflictError,
    StoredIntegrityError,
)


def register_domain_exception_handlers(application: FastAPI) -> None:
    """Install one explicit status mapping per public domain failure."""
    application.add_exception_handler(ResourceNotFoundError, _not_found_handler)
    application.add_exception_handler(ResourceRequestError, _resource_request_handler)
    application.add_exception_handler(SlugConflictError, _conflict_handler)
    application.add_exception_handler(InvalidStateTransitionError, _transition_handler)
    application.add_exception_handler(QuoteConflictError, _quote_conflict_handler)
    application.add_exception_handler(QuoteInputValidationError, _quote_input_handler)
    application.add_exception_handler(StoredIntegrityError, _stored_integrity_handler)
    application.add_exception_handler(PolicyTTLExceededError, _policy_ttl_handler)
    application.add_exception_handler(ApprovalNotFoundError, _approval_not_found_handler)
    application.add_exception_handler(ApprovalExpiredError, _approval_expired_handler)
    application.add_exception_handler(ApprovalVerificationError, _approval_verification_handler)
    application.add_exception_handler(ApprovalConflictError, _approval_conflict_handler)
    application.add_exception_handler(ApprovalIntegrityError, _approval_integrity_handler)
    application.add_exception_handler(
        AuthenticationUnauthorizedError,
        _authentication_unauthorized_handler,
    )
    application.add_exception_handler(
        AuthenticationForbiddenError,
        _authentication_forbidden_handler,
    )
    application.add_exception_handler(
        AuthenticationNotFoundError,
        _authentication_not_found_handler,
    )
    application.add_exception_handler(
        AuthenticationExpiredError,
        _authentication_expired_handler,
    )
    application.add_exception_handler(
        AuthenticationVerificationError,
        _authentication_verification_handler,
    )
    application.add_exception_handler(
        AuthenticationConflictError,
        _authentication_conflict_handler,
    )
    application.add_exception_handler(
        AuthenticationIntegrityError,
        _authentication_integrity_handler,
    )
    application.add_exception_handler(PaymentNotFoundError, _payment_not_found_handler)
    application.add_exception_handler(PaymentExpiredError, _payment_expired_handler)
    application.add_exception_handler(PaymentVerificationError, _payment_verification_handler)
    application.add_exception_handler(PaymentConflictError, _payment_conflict_handler)
    application.add_exception_handler(PaymentIntegrityError, _payment_integrity_handler)
    application.add_exception_handler(PaymentProviderError, _payment_provider_handler)
    application.add_exception_handler(PaymentUnavailableError, _payment_unavailable_handler)
    application.add_exception_handler(PaymentTimeoutError, _payment_timeout_handler)
    application.add_exception_handler(CompensationNotFoundError, _compensation_not_found_handler)
    application.add_exception_handler(CompensationForbiddenError, _compensation_forbidden_handler)
    application.add_exception_handler(CompensationConflictError, _compensation_conflict_handler)
    application.add_exception_handler(CompensationIntegrityError, _compensation_integrity_handler)
    application.add_exception_handler(
        CompensationUnavailableError,
        _compensation_unavailable_handler,
    )
    application.add_exception_handler(CompensationTimeoutError, _compensation_timeout_handler)
    application.add_exception_handler(EntitlementNotFoundError, _entitlement_not_found_handler)
    application.add_exception_handler(EntitlementForbiddenError, _entitlement_forbidden_handler)
    application.add_exception_handler(EntitlementExpiredError, _entitlement_expired_handler)
    application.add_exception_handler(EntitlementConflictError, _entitlement_conflict_handler)
    application.add_exception_handler(EntitlementIntegrityError, _entitlement_integrity_handler)
    application.add_exception_handler(
        EntitlementUnavailableError,
        _entitlement_unavailable_handler,
    )
    application.add_exception_handler(CapabilityInvalidError, _capability_unauthorized_handler)
    application.add_exception_handler(CapabilityExpiredError, _capability_unauthorized_handler)
    application.add_exception_handler(CapabilityForbiddenError, _capability_forbidden_handler)
    application.add_exception_handler(FulfillmentConflictError, _fulfillment_conflict_handler)
    application.add_exception_handler(FulfillmentRetryableError, _fulfillment_retryable_handler)
    application.add_exception_handler(FulfillmentPermanentError, _fulfillment_permanent_handler)
    application.add_exception_handler(FulfillmentIntegrityError, _fulfillment_integrity_handler)
    application.add_exception_handler(McpSessionUnauthorizedError, _mcp_unauthorized_handler)
    application.add_exception_handler(McpSessionForbiddenError, _mcp_forbidden_handler)
    application.add_exception_handler(McpSessionNotFoundError, _mcp_not_found_handler)
    application.add_exception_handler(McpSessionConflictError, _mcp_conflict_handler)


def _mcp_response(error: Exception, status_code: int) -> JSONResponse:
    assert isinstance(
        error,
        (
            McpSessionUnauthorizedError,
            McpSessionForbiddenError,
            McpSessionNotFoundError,
            McpSessionConflictError,
        ),
    )
    return JSONResponse(
        status_code=status_code,
        content={"detail": str(error), "reason_code": error.reason_code},
        headers={"Cache-Control": "private, no-store", "Pragma": "no-cache"},
    )


async def _mcp_unauthorized_handler(request: Request, error: Exception) -> JSONResponse:
    del request
    response = _mcp_response(error, status.HTTP_401_UNAUTHORIZED)
    response.headers["WWW-Authenticate"] = 'Bearer realm="metergate-mcp"'
    return response


async def _mcp_forbidden_handler(request: Request, error: Exception) -> JSONResponse:
    del request
    return _mcp_response(error, status.HTTP_403_FORBIDDEN)


async def _mcp_not_found_handler(request: Request, error: Exception) -> JSONResponse:
    del request
    return _mcp_response(error, status.HTTP_404_NOT_FOUND)


async def _mcp_conflict_handler(request: Request, error: Exception) -> JSONResponse:
    del request
    return _mcp_response(error, status.HTTP_409_CONFLICT)


async def _not_found_handler(
    request: Request,
    error: Exception,
) -> JSONResponse:
    del request
    return JSONResponse(
        status_code=status.HTTP_404_NOT_FOUND,
        content={"detail": str(error)},
    )


async def _resource_request_handler(request: Request, error: Exception) -> JSONResponse:
    del request
    assert isinstance(error, ResourceRequestError)
    status_code = (
        status.HTTP_413_CONTENT_TOO_LARGE
        if error.reason_code == "RESOURCE_REQUEST_BODY_TOO_LARGE"
        else status.HTTP_422_UNPROCESSABLE_CONTENT
    )
    return _paid_access_response(error, status_code)


async def _conflict_handler(
    request: Request,
    error: Exception,
) -> JSONResponse:
    del request
    return JSONResponse(
        status_code=status.HTTP_409_CONFLICT,
        content={"detail": str(error)},
    )


async def _transition_handler(
    request: Request,
    error: Exception,
) -> JSONResponse:
    del request
    return JSONResponse(
        status_code=status.HTTP_409_CONFLICT,
        content={"detail": str(error)},
    )


async def _quote_conflict_handler(
    request: Request,
    error: Exception,
) -> JSONResponse:
    del request
    assert isinstance(error, QuoteConflictError)
    return JSONResponse(
        status_code=status.HTTP_409_CONFLICT,
        content={"detail": str(error), "reason_code": error.reason_code},
    )


async def _quote_input_handler(
    request: Request,
    error: Exception,
) -> JSONResponse:
    del request
    assert isinstance(error, QuoteInputValidationError)
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        content={
            "detail": [
                {
                    "type": issue.error_type,
                    "loc": ["body", "input", *issue.path],
                    "msg": "Input does not satisfy the service schema",
                    "ctx": {"keyword": issue.keyword},
                }
                for issue in error.issues
            ]
        },
    )


async def _stored_integrity_handler(
    request: Request,
    error: Exception,
) -> JSONResponse:
    del request
    assert isinstance(error, StoredIntegrityError)
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"detail": str(error), "reason_code": error.reason_code},
    )


async def _policy_ttl_handler(
    request: Request,
    error: Exception,
) -> JSONResponse:
    del request
    assert isinstance(error, PolicyTTLExceededError)
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        content={
            "detail": [
                {
                    "type": "less_than_equal",
                    "loc": ["body", "expires_in_seconds"],
                    "msg": "Policy lifetime exceeds the configured maximum",
                    "ctx": {"le": error.maximum_seconds},
                }
            ]
        },
    )


def _approval_response(error: Exception, status_code: int) -> JSONResponse:
    assert isinstance(
        error,
        (
            ApprovalNotFoundError,
            ApprovalExpiredError,
            ApprovalVerificationError,
            ApprovalConflictError,
            ApprovalIntegrityError,
        ),
    )
    return JSONResponse(
        status_code=status_code,
        content={"detail": str(error), "reason_code": error.reason_code},
        headers={"Cache-Control": "private, no-store"},
    )


async def _approval_not_found_handler(request: Request, error: Exception) -> JSONResponse:
    del request
    return _approval_response(error, status.HTTP_404_NOT_FOUND)


async def _approval_expired_handler(request: Request, error: Exception) -> JSONResponse:
    del request
    return _approval_response(error, status.HTTP_410_GONE)


async def _approval_verification_handler(request: Request, error: Exception) -> JSONResponse:
    del request
    return _approval_response(error, status.HTTP_400_BAD_REQUEST)


async def _approval_conflict_handler(request: Request, error: Exception) -> JSONResponse:
    del request
    return _approval_response(error, status.HTTP_409_CONFLICT)


async def _approval_integrity_handler(request: Request, error: Exception) -> JSONResponse:
    del request
    return _approval_response(error, status.HTTP_500_INTERNAL_SERVER_ERROR)


def _authentication_response(error: Exception, status_code: int) -> JSONResponse:
    assert isinstance(
        error,
        (
            AuthenticationUnauthorizedError,
            AuthenticationForbiddenError,
            AuthenticationNotFoundError,
            AuthenticationExpiredError,
            AuthenticationVerificationError,
            AuthenticationConflictError,
            AuthenticationIntegrityError,
        ),
    )
    return JSONResponse(
        status_code=status_code,
        content={"detail": str(error), "reason_code": error.reason_code},
        headers={"Cache-Control": "private, no-store"},
    )


async def _authentication_unauthorized_handler(
    request: Request,
    error: Exception,
) -> JSONResponse:
    del request
    return _authentication_response(error, status.HTTP_401_UNAUTHORIZED)


async def _authentication_forbidden_handler(request: Request, error: Exception) -> JSONResponse:
    del request
    return _authentication_response(error, status.HTTP_403_FORBIDDEN)


async def _authentication_not_found_handler(request: Request, error: Exception) -> JSONResponse:
    del request
    return _authentication_response(error, status.HTTP_404_NOT_FOUND)


async def _authentication_expired_handler(request: Request, error: Exception) -> JSONResponse:
    del request
    return _authentication_response(error, status.HTTP_410_GONE)


async def _authentication_verification_handler(
    request: Request,
    error: Exception,
) -> JSONResponse:
    del request
    return _authentication_response(error, status.HTTP_400_BAD_REQUEST)


async def _authentication_conflict_handler(request: Request, error: Exception) -> JSONResponse:
    del request
    return _authentication_response(error, status.HTTP_409_CONFLICT)


async def _authentication_integrity_handler(request: Request, error: Exception) -> JSONResponse:
    del request
    return _authentication_response(error, status.HTTP_500_INTERNAL_SERVER_ERROR)


def _payment_response(error: Exception, status_code: int) -> JSONResponse:
    assert isinstance(
        error,
        (
            PaymentNotFoundError,
            PaymentExpiredError,
            PaymentVerificationError,
            PaymentConflictError,
            PaymentIntegrityError,
            PaymentProviderError,
            PaymentUnavailableError,
            PaymentTimeoutError,
        ),
    )
    return JSONResponse(
        status_code=status_code,
        content={"detail": str(error), "reason_code": error.reason_code},
        headers={"Cache-Control": "private, no-store"},
    )


async def _payment_not_found_handler(request: Request, error: Exception) -> JSONResponse:
    del request
    return _payment_response(error, status.HTTP_404_NOT_FOUND)


async def _payment_expired_handler(request: Request, error: Exception) -> JSONResponse:
    del request
    return _payment_response(error, status.HTTP_410_GONE)


async def _payment_verification_handler(request: Request, error: Exception) -> JSONResponse:
    del request
    return _payment_response(error, status.HTTP_400_BAD_REQUEST)


async def _payment_conflict_handler(request: Request, error: Exception) -> JSONResponse:
    del request
    return _payment_response(error, status.HTTP_409_CONFLICT)


async def _payment_integrity_handler(request: Request, error: Exception) -> JSONResponse:
    del request
    return _payment_response(error, status.HTTP_500_INTERNAL_SERVER_ERROR)


async def _payment_provider_handler(request: Request, error: Exception) -> JSONResponse:
    del request
    return _payment_response(error, status.HTTP_502_BAD_GATEWAY)


async def _payment_unavailable_handler(request: Request, error: Exception) -> JSONResponse:
    del request
    return _payment_response(error, status.HTTP_503_SERVICE_UNAVAILABLE)


async def _payment_timeout_handler(request: Request, error: Exception) -> JSONResponse:
    del request
    return _payment_response(error, status.HTTP_504_GATEWAY_TIMEOUT)


def _compensation_response(error: Exception, status_code: int) -> JSONResponse:
    assert isinstance(
        error,
        (
            CompensationNotFoundError,
            CompensationForbiddenError,
            CompensationConflictError,
            CompensationIntegrityError,
            CompensationUnavailableError,
            CompensationTimeoutError,
        ),
    )
    return JSONResponse(
        status_code=status_code,
        content={"detail": str(error), "reason_code": error.reason_code},
        headers={"Cache-Control": "private, no-store"},
    )


async def _compensation_not_found_handler(request: Request, error: Exception) -> JSONResponse:
    del request
    return _compensation_response(error, status.HTTP_404_NOT_FOUND)


async def _compensation_forbidden_handler(request: Request, error: Exception) -> JSONResponse:
    del request
    return _compensation_response(error, status.HTTP_403_FORBIDDEN)


async def _compensation_conflict_handler(request: Request, error: Exception) -> JSONResponse:
    del request
    return _compensation_response(error, status.HTTP_409_CONFLICT)


async def _compensation_integrity_handler(request: Request, error: Exception) -> JSONResponse:
    del request
    return _compensation_response(error, status.HTTP_500_INTERNAL_SERVER_ERROR)


async def _compensation_unavailable_handler(request: Request, error: Exception) -> JSONResponse:
    del request
    return _compensation_response(error, status.HTTP_503_SERVICE_UNAVAILABLE)


async def _compensation_timeout_handler(request: Request, error: Exception) -> JSONResponse:
    del request
    return _compensation_response(error, status.HTTP_504_GATEWAY_TIMEOUT)


def _paid_access_response(
    error: Exception,
    status_code: int,
    *,
    bearer_challenge: bool = False,
) -> JSONResponse:
    reason_code = getattr(error, "reason_code", "PAID_ACCESS_FAILED")
    headers = {"Cache-Control": "private, no-store", "Pragma": "no-cache"}
    if bearer_challenge:
        headers["WWW-Authenticate"] = 'Bearer realm="metergate-protected-resource"'
    return JSONResponse(
        status_code=status_code,
        content={"detail": str(error), "reason_code": reason_code},
        headers=headers,
    )


async def _entitlement_not_found_handler(request: Request, error: Exception) -> JSONResponse:
    del request
    return _paid_access_response(error, status.HTTP_404_NOT_FOUND)


async def _entitlement_forbidden_handler(request: Request, error: Exception) -> JSONResponse:
    del request
    return _paid_access_response(error, status.HTTP_403_FORBIDDEN)


async def _entitlement_expired_handler(request: Request, error: Exception) -> JSONResponse:
    del request
    return _paid_access_response(error, status.HTTP_410_GONE)


async def _entitlement_conflict_handler(request: Request, error: Exception) -> JSONResponse:
    del request
    return _paid_access_response(error, status.HTTP_409_CONFLICT)


async def _entitlement_integrity_handler(request: Request, error: Exception) -> JSONResponse:
    del request
    return _paid_access_response(error, status.HTTP_500_INTERNAL_SERVER_ERROR)


async def _entitlement_unavailable_handler(request: Request, error: Exception) -> JSONResponse:
    del request
    return _paid_access_response(error, status.HTTP_503_SERVICE_UNAVAILABLE)


async def _capability_unauthorized_handler(request: Request, error: Exception) -> JSONResponse:
    del request
    return _paid_access_response(
        error,
        status.HTTP_401_UNAUTHORIZED,
        bearer_challenge=True,
    )


async def _capability_forbidden_handler(request: Request, error: Exception) -> JSONResponse:
    del request
    return _paid_access_response(error, status.HTTP_403_FORBIDDEN)


async def _fulfillment_conflict_handler(request: Request, error: Exception) -> JSONResponse:
    del request
    return _paid_access_response(error, status.HTTP_409_CONFLICT)


async def _fulfillment_retryable_handler(request: Request, error: Exception) -> JSONResponse:
    del request
    return _paid_access_response(error, status.HTTP_503_SERVICE_UNAVAILABLE)


async def _fulfillment_permanent_handler(request: Request, error: Exception) -> JSONResponse:
    del request
    status_code = (
        status.HTTP_503_SERVICE_UNAVAILABLE
        if getattr(error, "reason_code", "") == "FULFILLMENT_PROVIDER_UNAVAILABLE"
        else status.HTTP_409_CONFLICT
    )
    return _paid_access_response(error, status_code)


async def _fulfillment_integrity_handler(request: Request, error: Exception) -> JSONResponse:
    del request
    return _paid_access_response(error, status.HTTP_502_BAD_GATEWAY)
