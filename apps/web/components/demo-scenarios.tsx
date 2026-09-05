"use client";

import { useState } from "react";

const scenarios = [
  {
    title: "Buy a useful report", label: "01 · Successful delivery", outcome: "Compare → approve → pay → read the report",
    prompt: "Use only the registered MeterGate MCP tools. I need a useful orbital-condition report for NORAD 25544, one-time only, with a hard ₹6 maximum. Compare the relevant catalog services using their published scope and price, explain your choice briefly, request its quote, create the bounded policy, and evaluate it. Stop at the human approval link. After I complete approval and Razorpay Test Mode Checkout, resume from get_purchase_status, respect retry_after_seconds, and retrieve the purchased resource. Never disclose credentials.",
  },
  {
    title: "Enforce the budget", label: "02 · Deterministic rejection", outcome: "₹9 service + ₹5 maximum → purchase denied",
    prompt: "Use only the registered MeterGate MCP tools. I specifically require Detailed Orbital Analysis for NORAD 25544, one-time only, with a hard ₹5 maximum. Do not substitute another service. Request the server quote, create the ₹5 policy, evaluate it, and report the authoritative reason codes. Stop when denied; do not raise the budget or proceed to payment.",
  },
  {
    title: "Recover a failed delivery", label: "03 · Backend compensation", outcome: "Verified payment → failed delivery → processed refund",
    prompt: "Use only the registered MeterGate MCP tools. This is the operator-prepared Test Mode failure demonstration. Request Orbital Risk Report for NORAD 25544, one-time only, with a hard ₹6 maximum. Evaluate the quote and stop for human approval and checkout. After I pay, resume purchase status and attempt the purchased resource when access is ready. If fulfillment fails, report the failure and poll get_purchase_status no faster than retry_after_seconds until the backend reports refunded or requires human attention. Never attempt to authorize a refund yourself or report pending as completed.",
  },
];

export function DemoScenarios() {
  const [message, setMessage] = useState("");
  async function copy(index: number) {
    try { await navigator.clipboard.writeText(scenarios[index].prompt); setMessage(`Copied: ${scenarios[index].title}`); }
    catch { setMessage("Copy is unavailable in this browser. Expand the prompt and select its text."); }
  }
  return <section id="demo-guide" className="content-section demo-guide">
    <div className="site-container">
      <div className="section-head"><h2>One purchase.<br/>Three outcomes worth proving.</h2><p>Use a connected AI buyer to explore the real commerce flow. These prompts start actual Test Mode scenarios; each outcome is verified by the server.</p></div>
      <ol className="demo-cards">{scenarios.map((scenario, index) => <li key={scenario.title}>
        <p className="eyebrow">{scenario.label}</p><h3>{scenario.title}</h3><p>{scenario.outcome}</p>
        <button className="button" type="button" onClick={() => void copy(index)}>Copy agent prompt</button>
        <details className="scenario-prompt"><summary>Read prompt</summary><p>{scenario.prompt}</p></details>
      </li>)}</ol>
      <p role="status" className="copy-status">{message}</p>
      <div className="demo-setup"><p><strong>Start here:</strong> sign in with a passkey and configure the MeterGate MCP connection once. On later visits, renew the existing connection’s access without replacing its key. Human approval and Razorpay Checkout stay in your browser.</p><a className="button primary" href="#buyer-account">Connect your buyer</a><a className="button" href="/operator">Inspect the audit trail</a></div>
      <p className="report-note">Failure recovery requires an operator to configure the development merchant’s permanent fault mode and enable Test Mode refunds before the run. This page cannot trigger faults or refunds. Restore normal merchant operation afterwards.</p>
    </div>
  </section>;
}
