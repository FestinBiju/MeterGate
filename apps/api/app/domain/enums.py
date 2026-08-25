"""Stable persisted values for the initial merchant catalog domain."""

from enum import StrEnum


class AccountStatus(StrEnum):
    PENDING = "pending"
    ACTIVE = "active"
    DISABLED = "disabled"


class ApprovalIdentityStatus(StrEnum):
    ACTIVE = "active"
    DISABLED = "disabled"


class MerchantStatus(StrEnum):
    ACTIVE = "active"
    INACTIVE = "inactive"
    SUSPENDED = "suspended"


class ServiceStatus(StrEnum):
    DRAFT = "draft"
    ACTIVE = "active"
    INACTIVE = "inactive"
    ARCHIVED = "archived"


class ServiceType(StrEnum):
    API = "api"
    REPORT = "report"
    DATASET = "dataset"
    INFERENCE = "inference"
    DIGITAL_ASSET = "digital_asset"
    OTHER = "other"


class PurchaseType(StrEnum):
    ONE_TIME = "one_time"
    SUBSCRIPTION = "subscription"
    USAGE_BASED = "usage_based"


class PolicyDecision(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
