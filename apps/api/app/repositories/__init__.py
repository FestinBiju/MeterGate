"""Persistence repositories for canonical domain records."""

from app.repositories.merchants import MerchantRepository
from app.repositories.quotes import QuoteRepository
from app.repositories.services import ServiceRepository

__all__ = ["MerchantRepository", "QuoteRepository", "ServiceRepository"]
