type RecordValue = Record<string, unknown>;
function record(value: unknown): RecordValue {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("Receipt evidence is invalid");
  return value as RecordValue;
}
function text(value: unknown): string {
  if (typeof value !== "string" || !value) throw new Error("Receipt evidence is incomplete");
  return value;
}
function optionalText(value: unknown): string | null { return typeof value === "string" ? value : null; }
function amount(value: unknown): number {
  if (typeof value !== "number" || !Number.isSafeInteger(value) || value < 0) throw new Error("Receipt amount is invalid");
  return value;
}
function party(value: unknown) { const p = record(value); return { id: text(p.id), name: text(p.name) }; }

export function buildPurchaseReceipt(
  transactionValue: unknown, authorizationValue: unknown, evaluationValue: unknown,
  resultEvidence: { transaction_id: string; result_hash: string; completed_at: string } | null,
  exportedAt: string,
) {
  const transaction = record(transactionValue);
  const authorization = record(authorizationValue);
  const evaluation = record(evaluationValue);
  const id = text(transaction.transaction_id);
  if (!/^txn_[0-7][0-9A-HJKMNP-TV-Z]{25}$/.test(id) ||
      transaction.authorization_id !== authorization.id ||
      authorization.evaluation_id !== evaluation.id ||
      transaction.amount !== authorization.amount || transaction.currency !== authorization.currency ||
      party(transaction.merchant).id !== party(authorization.merchant).id ||
      party(transaction.service).id !== party(authorization.service).id) {
    throw new Error("Receipt evidence does not belong to one purchase");
  }
  if (resultEvidence && (resultEvidence.transaction_id !== id ||
      !/^sha256:[0-9a-f]{64}$/.test(resultEvidence.result_hash) ||
      !Number.isFinite(Date.parse(resultEvidence.completed_at)))) {
    throw new Error("Receipt result evidence is invalid");
  }
  const refund = transaction.refund_summary ? record(transaction.refund_summary) : null;
  const compensation = transaction.compensation_summary ? record(transaction.compensation_summary) : null;
  const checks = Array.isArray(evaluation.checks) ? evaluation.checks.map(value => {
    const check = record(value);
    return { rule: text(check.rule), result: text(check.result), reason_code: text(check.reason_code) };
  }) : [];
  // Explicit field projection: never spread a transaction, checkout, credential, or raw result.
  return {
    receipt_version: "1",
    mode: "Razorpay Test Mode",
    notice: "Purchase evidence snapshot. Not a tax invoice or a signed attestation. No real money moved.",
    exported_at: exportedAt,
    transaction_id: id,
    merchant: party(transaction.merchant), service: party(transaction.service),
    amount_minor: amount(transaction.amount), currency: text(transaction.currency),
    purchase_type: text(transaction.purchase_type),
    quote_id: text(evaluation.quote_id), quote_hash: text(authorization.quote_hash),
    policy_id: text(evaluation.policy_id), policy_decision: text(evaluation.decision), policy_checks: checks,
    approval: { authorization_id: text(authorization.id), authorized_at: text(authorization.authorized_at) },
    payment: {
      state: text(transaction.state), provider_order_id: optionalText(transaction.provider_order_id),
      created_at: optionalText(transaction.created_at), paid_at: optionalText(transaction.paid_at),
    },
    commerce_outcome: text(transaction.commerce_outcome),
    fulfillment: transaction.commerce_outcome === "fulfilled" && resultEvidence
      ? { result_hash: resultEvidence.result_hash, completed_at: resultEvidence.completed_at }
      : null,
    compensation: compensation ? { decision_state: text(compensation.decision_state), reason_code: text(compensation.decision_reason_code) } : null,
    refund: refund ? {
      refund_id: text(refund.refund_id), amount_minor: amount(refund.amount), currency: text(refund.currency),
      state: text(refund.state), provider_status: optionalText(refund.provider_status),
      provider_refund_id: optionalText(refund.provider_refund_id),
    } : null,
  };
}
