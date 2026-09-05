type Fields = Record<string, unknown>;
export type ReportField = { label: string; value: string };
export type OrbitReport = {
  title: string; identity: string; fields: ReportField[]; disclaimer: string;
};

function record(value: unknown): Fields {
  return value !== null && typeof value === "object" && !Array.isArray(value) ? value as Fields : {};
}

export function describeOrbitResult(value: unknown): OrbitReport | null {
  const result = record(value);
  const titles: Record<string, string> = {
    satellite_status: "Satellite status",
    orbital_risk_report: "Orbital condition report",
    detailed_orbital_analysis: "Detailed orbital analysis",
  };
  if (typeof result.result_type !== "string" || !Object.hasOwn(titles, result.result_type)) return null;
  const detailed = result.result_type === "detailed_orbital_analysis";
  const source = detailed ? record(result.source) : result;
  if (typeof source.object_name !== "string" || !Number.isSafeInteger(source.norad_id)) return null;
  const orbit = detailed ? record(result.derived) : result;
  const freshness = detailed ? record(result.freshness) : result;
  const indicators = detailed ? record(result.heuristic_indicators) : result;
  const fields: ReportField[] = [];
  function text(label: string, value: unknown) {
    if (typeof value === "string" && value.trim()) fields.push({ label, value: value.replaceAll("_", " ") });
  }
  function number(label: string, value: unknown, unit: string) {
    if (typeof value === "number" && Number.isFinite(value)) {
      fields.push({ label, value: `${new Intl.NumberFormat("en-IN", { maximumFractionDigits: 3 }).format(value)}${unit ? ` ${unit}` : ""}` });
    }
  }
  const epoch = source.epoch ?? result.source_epoch;
  if (typeof epoch === "string" && Number.isFinite(Date.parse(epoch))) {
    fields.push({ label: "Source epoch (UTC)", value: new Date(epoch).toISOString().replace("T", " ") });
  }
  text("Source", result.data_source);
  text("Orbital regime", orbit.orbital_regime);
  number("Orbital period", orbit.orbital_period_minutes, "min");
  number("Approx. perigee altitude", orbit.approximate_perigee_altitude_km, "km");
  number("Approx. apogee altitude", orbit.approximate_apogee_altitude_km, "km");
  number("Semi-major axis", orbit.semi_major_axis_km, "km");
  number("Inclination", source.inclination ?? source.inclination_degrees, "°");
  number("Mean motion", source.mean_motion ?? source.mean_motion_rev_per_day, "rev/day");
  number("Element set age at calculation", freshness.element_set_age_hours, "hours");
  text("Freshness at calculation", freshness.data_freshness_indicator);
  text("Eccentricity indicator", indicators.eccentricity_indicator);
  if (typeof indicators.very_low_perigee_indicator === "boolean") {
    fields.push({ label: "Very low perigee indicator", value: indicators.very_low_perigee_indicator ? "Flagged by heuristic" : "Not flagged by heuristic" });
  }
  return {
    title: titles[result.result_type],
    identity: `${source.object_name} · NORAD ${source.norad_id}`,
    fields,
    disclaimer: typeof result.disclaimer === "string" ? result.disclaimer : "Informational orbital snapshot. This is not conjunction screening, a collision warning, or an operational tracking service.",
  };
}
