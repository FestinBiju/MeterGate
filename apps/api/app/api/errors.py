"""HTTP mappings for safe, expected domain failures."""

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse

from app.domain.exceptions import (
    InvalidStateTransitionError,
    ResourceNotFoundError,
    SlugConflictError,
)


def register_domain_exception_handlers(application: FastAPI) -> None:
    """Install one explicit status mapping per public domain failure."""
    application.add_exception_handler(ResourceNotFoundError, _not_found_handler)
    application.add_exception_handler(SlugConflictError, _conflict_handler)
    application.add_exception_handler(InvalidStateTransitionError, _transition_handler)


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
