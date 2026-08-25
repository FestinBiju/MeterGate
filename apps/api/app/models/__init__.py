"""ORM model registry."""

from app.models.base import Base
from app.models.merchant import Merchant
from app.models.service import Service

__all__ = ["Base", "Merchant", "Service"]
