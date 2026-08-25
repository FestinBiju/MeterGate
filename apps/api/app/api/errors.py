"""HTTP mappings for safe, expected domain failures."""

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse

from app.domain.exceptions import (
    InvalidStateTransitionError,
    QuoteConflictError,
    QuoteInputValidationError,
    QuoteIntegrityError,
    ResourceNotFoundError,
    SlugConflictError,
)


def register_domain_exception_handlers(application: FastAPI) -> None:
    """Install one explicit status mapping per public domain failure."""
    application.add_exception_handler(ResourceNotFoundError, _not_found_handler)
    application.add_exception_handler(SlugConflictError, _conflict_handler)
    application.add_exception_handler(InvalidStateTransitionError, _transition_handler)
    application.add_exception_handler(QuoteConflictError, _quote_conflict_handler)
    application.add_exception_handler(QuoteInputValidationError, _quote_input_handler)
    application.add_exception_handler(QuoteIntegrityError, _quote_integrity_handler)


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


async def _quote_integrity_handler(
    request: Request,
    error: Exception,
) -> JSONResponse:
    del request, error
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"detail": "Stored quote failed integrity verification"},
    )
