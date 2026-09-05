import { historicalRefund, policyEvidence } from "@/lib/submission-evidence";
import Link from "next/link";

export default function EvidencePage() {
  return <div className="minimal-shell"><main className="site-container evidence-page">
    <Link href="/">← MeterGate</Link><p className="eyebrow">Evidence and limitations</p>
    <h1>What we have measured.</h1>
    <p>Policy evaluation, real provider acceptance, and live product metrics answer different questions. Their scopes are recorded here.</p>
    <section className="content-section"><h2>{policyEvidence.passed}/{policyEvidence.scenario_count} policy scenarios passed</h2>
      <p>Generated {policyEvidence.generated_at}. This is an in-process evaluation of the production deterministic policy engine. It does not measure an LLM’s service-selection accuracy or complete payment journeys.</p>
      <p>Revision <code>{policyEvidence.app_commit_hash}</code>; working tree {policyEvidence.working_tree_dirty ? "had uncommitted changes" : "was clean"} at generation.</p>
      <p>The five cases labelled prompt injection are over-budget quote proposals. They verify that deterministic price policy rejects those proposals; they do not constitute a general prompt-injection benchmark.</p>
      <details className="raw-evidence"><summary>Inspect all recorded policy scenarios</summary><div className="table-scroll"><table><thead><tr><th>Scenario</th><th>Expected</th><th>Observed</th><th>Result</th></tr></thead><tbody>{policyEvidence.scenarios.map(row => <tr key={row.id}><td>{row.id}</td><td>{row.expected}</td><td>{row.actual}</td><td>{row.passed ? "Pass" : "Fail"}</td></tr>)}</tbody></table></div></details>
    </section>
    <section className="content-section"><h2>One real Test Mode refund accepted</h2>
      <p>On {historicalRefund.date}, one ₹5 payment with no delivered result converged to a Razorpay refund with provider status <strong>{historicalRefund.providerStatus}</strong>. This was a historical acceptance run after entitlement expiry, not a live measurement on this page.</p>
      <p>The {historicalRefund.requestToCompletionSeconds}-second interval measures refund request start to local completion. It excludes the earlier payment, entitlement expiry, and waiting time. Provider reference: <code>{historicalRefund.refundId}</code>.</p>
      <p>Convergence used an authoritative provider API read; no refund webhook was received during that acceptance window. One successful run does not establish a population recovery rate. No real money moved.</p>
    </section>
    <section className="content-section"><h2>Where AI acts</h2><p>The connected AI client interprets intent, compares catalog entries, and proposes a purchase. The bundled reference CLI uses keyword matching. Neither can bypass deterministic policy, passkey approval, payment verification, entitlement restrictions, or refund rules.</p><p>Current GMV, worker health, transactions, and recovery evidence are available to authorized operators. Missing public metrics are not estimated.</p><a className="button" href="/operator">Open operator evidence</a></section>
    <Link className="button primary" href="/#demo-guide">Try the guided scenarios</Link>
  </main></div>;
}
