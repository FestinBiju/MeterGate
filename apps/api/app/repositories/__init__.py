"""Persistence repositories for canonical domain records."""

from app.repositories.accounts import AccountRepository
from app.repositories.approval_identities import ApprovalIdentityRepository
from app.repositories.authorizations import PurchaseAuthorizationRepository
from app.repositories.buyer_policies import BuyerPolicyRepository
from app.repositories.merchants import MerchantRepository
from app.repositories.passkey_credentials import PasskeyCredentialRepository
from app.repositories.policy_evaluations import PolicyEvaluationRepository
from app.repositories.quotes import QuoteRepository
from app.repositories.services import ServiceRepository

__all__ = [
    "AccountRepository",
    "ApprovalIdentityRepository",
    "BuyerPolicyRepository",
    "MerchantRepository",
    "PasskeyCredentialRepository",
    "PolicyEvaluationRepository",
    "PurchaseAuthorizationRepository",
    "QuoteRepository",
    "ServiceRepository",
]
