"use client";

import { useState } from "react";
import { useAccountSession } from "@/components/account-session";
import { buildPurchaseReceipt } from "@/lib/purchase-receipt";

export function PurchaseReceipt({ apiBaseEndpoint, transactionId, resultEvidence = null }: {
  apiBaseEndpoint: string; transactionId: string;
  resultEvidence?: { transaction_id: string; result_hash: string; completed_at: string } | null;
}) {
  const { requestAuthenticated } = useAccountSession();
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  async function download() {
    setBusy(true); setMessage("");
    try {
      const transaction = await requestAuthenticated(`${apiBaseEndpoint}/payment-transactions/${encodeURIComponent(transactionId)}`, { method: "GET" });
      if (transaction.transaction_id !== transactionId || typeof transaction.authorization_id !== "string") throw new Error("Invalid transaction");
      const authorization = await requestAuthenticated(`${apiBaseEndpoint}/authorizations/${encodeURIComponent(transaction.authorization_id)}`, { method: "GET" });
      if (typeof authorization.evaluation_id !== "string") throw new Error("Invalid authorization");
      const evaluation = await requestAuthenticated(`${apiBaseEndpoint}/policy-evaluations/${encodeURIComponent(authorization.evaluation_id)}`, { method: "GET" });
      const receipt = buildPurchaseReceipt(transaction, authorization, evaluation, resultEvidence, new Date().toISOString());
      const url = URL.createObjectURL(new Blob([JSON.stringify(receipt, null, 2)], { type: "application/json" }));
      const link = document.createElement("a");
      link.href = url; link.download = `metergate-${transactionId}.json`;
      document.body.appendChild(link); link.click(); link.remove();
      URL.revokeObjectURL(url);
      setMessage("Receipt downloaded from freshly requested purchase evidence.");
    } catch {
      setMessage("Receipt could not be verified. Refresh your session and try again.");
    } finally { setBusy(false); }
  }
  return <div className="receipt-action">
    <button type="button" className="button" disabled={busy} onClick={() => void download()}>{busy ? "Preparing receipt…" : "Download purchase receipt"}</button>
    <p className="report-note">Test Mode · JSON evidence snapshot · not a tax invoice</p>
    <p role="status" className="report-note">{message}</p>
  </div>;
}
