"""Strict internal protocol, source snapshot, and service result models."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StringConstraints

FulfillmentExecutionId = Annotated[
    str,
    StringConstraints(pattern=r"^ful_[0-7][0-9A-HJKMNP-TV-Z]{25}$"),
]
ServiceId = Annotated[
    str,
    StringConstraints(pattern=r"^svc_[0-7][0-9A-HJKMNP-TV-Z]{25}$"),
]
InputHash = Annotated[
    str,
    StringConstraints(pattern=r"^sha256:[0-9a-f]{64}$"),
]
ServiceSlug = Literal[
    "satellite-status-lookup",
    "orbital-risk-report",
    "detailed-orbital-analysis",
]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class OrbitInput(StrictModel):
    norad_id: Annotated[StrictInt, Field(ge=1, le=999_999_999)]


class FulfillmentRequest(StrictModel):
    service_id: ServiceId
    service_slug: ServiceSlug
    input: OrbitInput
    input_hash: InputHash


class GPSnapshot(StrictModel):
    norad_id: int
    object_name: str
    international_designator: str | None
    epoch: datetime
    mean_motion: float
    eccentricity: float
    inclination: float
    raan: float
    argument_of_pericenter: float
    mean_anomaly: float
    classification_type: str | None = None
    element_set_number: int | None = None
    revolution_number_at_epoch: int | None = None
    bstar: float | None = None
    mean_motion_dot: float | None = None
    mean_motion_ddot: float | None = None
    fetched_at: datetime


class SourceFields(StrictModel):
    norad_id: int
    object_name: str
    international_designator: str | None = None
    epoch: datetime
    mean_motion_rev_per_day: float
    eccentricity: float
    inclination_degrees: float
    raan_degrees: float
    argument_of_pericenter_degrees: float
    mean_anomaly_degrees: float
    classification_type: str | None = None
    element_set_number: int | None = None
    revolution_number_at_epoch: int | None = None
    bstar: float | None = None
    mean_motion_dot: float | None = None
    mean_motion_ddot: float | None = None


class DerivedOrbit(StrictModel):
    orbital_period_minutes: float
    semi_major_axis_km: float
    approximate_perigee_altitude_km: float
    approximate_apogee_altitude_km: float
    orbital_regime: str


class FreshnessAnalysis(StrictModel):
    element_set_age_hours: float
    data_freshness_indicator: str


class HeuristicIndicators(StrictModel):
    eccentricity_indicator: str
    very_low_perigee_indicator: bool


class CalculationMetadata(StrictModel):
    model: Literal["two_body_keplerian_approximation"]
    earth_gravitational_parameter_km3_s2: float
    earth_reference_radius_km: float
    formulas: dict[str, str]
    approximation_notes: list[str]


class SatelliteStatusResult(StrictModel):
    result_type: Literal["satellite_status"] = "satellite_status"
    norad_id: int
    object_name: str
    international_designator: str | None = None
    epoch: datetime
    mean_motion: float
    eccentricity: float
    inclination: float
    raan: float
    argument_of_pericenter: float
    mean_anomaly: float
    data_source: Literal["CelesTrak"] = "CelesTrak"
    fetched_at: datetime


class OrbitalRiskResult(StrictModel):
    result_type: Literal["orbital_risk_report"] = "orbital_risk_report"
    norad_id: int
    object_name: str
    international_designator: str | None = None
    source_epoch: datetime
    element_set_age_hours: float
    orbital_period_minutes: float
    semi_major_axis_km: float
    approximate_perigee_altitude_km: float
    approximate_apogee_altitude_km: float
    orbital_regime: str
    eccentricity_indicator: str
    very_low_perigee_indicator: bool
    data_freshness_indicator: str
    assessment_scope: Literal["heuristic_orbital_condition"] = "heuristic_orbital_condition"
    operational_collision_assessment: Literal[False] = False
    disclaimer: str
    data_source: Literal["CelesTrak"] = "CelesTrak"
    fetched_at: datetime
    result_timestamp: datetime


class DetailedOrbitalAnalysisResult(StrictModel):
    result_type: Literal["detailed_orbital_analysis"] = "detailed_orbital_analysis"
    source: SourceFields
    derived: DerivedOrbit
    freshness: FreshnessAnalysis
    heuristic_indicators: HeuristicIndicators
    calculation_metadata: CalculationMetadata
    assessment_scope: Literal["heuristic_orbital_condition"] = "heuristic_orbital_condition"
    operational_collision_assessment: Literal[False] = False
    disclaimer: str
    data_source: Literal["CelesTrak"] = "CelesTrak"
    source_timestamp: datetime
    fetched_at: datetime
    result_timestamp: datetime


OrbitIntelResult = SatelliteStatusResult | OrbitalRiskResult | DetailedOrbitalAnalysisResult


class FulfillmentResponse(StrictModel):
    fulfillment_execution_id: FulfillmentExecutionId
    service_id: ServiceId
    input_hash: InputHash
    result_content_type: Literal["application/json"] = "application/json"
    result: OrbitIntelResult


class ErrorResponse(StrictModel):
    detail: str
    reason_code: str


JSONObject = dict[str, Any]
