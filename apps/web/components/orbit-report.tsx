import { describeOrbitResult } from "@/lib/orbit-report";

export function OrbitReport({ result }: { result: unknown }) {
  const report = describeOrbitResult(result);
  if (!report) return <p className="report-note">The purchased result is available in the raw evidence below. A summary is not available for this result format.</p>;
  return <section className="purchased-report" aria-label={report.title}>
    <p className="eyebrow">Delivered resource</p>
    <h3>{report.title}</h3>
    <p className="report-identity">{report.identity}</p>
    <dl className="report-grid">{report.fields.map(field => <div key={field.label}><dt>{field.label}</dt><dd>{field.value}</dd></div>)}</dl>
    <p className="report-note">{report.disclaimer}</p>
    <p className="report-note">Values describe the purchased snapshot; replaying a result does not fetch a new observation.</p>
  </section>;
}
