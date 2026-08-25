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
    InvalidStateTransitionError,
    PolicyTTLExceededError,
    QuoteConflictError,
    QuoteInputValidationError,
    ResourceNotFoundError,
    SlugConflictError,
    StoredIntegrityError,
)


def register_domain_exception_handlers(application: FastAPI) -> None:
    """Install one explicit status mapping per public domain failure."""
    application.add_exception_handler(ResourceNotFoundError, _not_found_handler)
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


async def _not_found_handler(
    request: Request,
    error: Exception,
) -> JSONResponse:
    del request
    return JSONResponse(
        status_code=status.HTTP_404_NOT_FOUND,
        content={"detail": str(error)},
    )


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
