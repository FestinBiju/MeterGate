import type { JsonValue } from "./schemas.js";

export type ToolCaller = {
  call(name: string, input: Record<string, unknown>): Promise<Record<string, unknown>>;
};

export type BuyerIntent = {
  request: string;
  maximumAmount: number;
  currency: string;
  purchaseType: "one_time";
  input: JsonValue;
};

export type ServiceProposal = {
  merchantId: string;
  merchantSlug: string;
  serviceId: string;
  serviceSlug: string;
};

export type BuyerPlanner = {
  proposeService(intent: BuyerIntent, catalog: Record<string, unknown>): ServiceProposal;
};

export type PurchaseContext = ServiceProposal & {
  input: JsonValue;
  maximumAmount: number;
  currency: string;
  purchaseType: "one_time";
  quoteId: string;
  policyId: string;
  evaluationId: string;
};

export type BuyerResult =
  | { state: "denied"; reasonCodes: string[]; context: PurchaseContext }
  | { state: "human_approval_required"; approvalUrl: string; context: PurchaseContext }
  | { state: "waiting"; reasonCode: string; retryAfterSeconds?: number; context: PurchaseContext }
  | { state: "payment_failed"; reasonCode: string; context: PurchaseContext }
  | { state: "refunded" | "compensation_pending" | "manual_review"; reasonCode: string; context: PurchaseContext }
  | { state: "fulfilled"; result: unknown; resultHash: string; replayedResult: boolean; context: PurchaseContext };

export class ReferenceBuyerAgent {
  constructor(
    private readonly tools: ToolCaller,
    private readonly planner: BuyerPlanner = new KeywordBuyerPlanner(),
  ) {}

  async begin(intent: BuyerIntent): Promise<BuyerResult> {
    const catalog = await this.tools.call("list_services", {});
    const proposal = this.planner.proposeService(intent, catalog);
    const quote = await this.tools.call("request_quote", {
      service_id: proposal.serviceId,
      input: intent.input,
    });
    const quoteId = requiredString(quote, "id");
    const policy = await this.tools.call("create_buyer_policy", {
      maximum_amount: intent.maximumAmount,
      allowed_currencies: [intent.currency],
      allowed_merchant_ids: [proposal.merchantId],
      allowed_service_ids: [proposal.serviceId],
      allowed_purchase_types: [intent.purchaseType],
      expires_in_seconds: 900,
    });
    const policyId = requiredString(policy, "id");
    const evaluated = await this.tools.call("evaluate_quote", {
      policy_id: policyId,
      quote_id: quoteId,
    });
    const evaluation = requiredRecord(evaluated, "evaluation");
    const evaluationId = requiredString(evaluation, "id");
    const context: PurchaseContext = {
      ...proposal,
      input: intent.input,
      maximumAmount: intent.maximumAmount,
      currency: intent.currency,
      purchaseType: intent.purchaseType,
      quoteId,
      policyId,
      evaluationId,
    };
    if (evaluated.state === "denied") {
      const reasonCodes = Array.isArray(evaluation.reason_codes)
        ? evaluation.reason_codes.filter((value): value is string => typeof value === "string")
        : ["POLICY_DENIED"];
      return { state: "denied", reasonCodes, context };
    }
    return {
      state: "human_approval_required",
      approvalUrl: requiredString(evaluated, "approval_url"),
      context,
    };
  }

  async resume(context: PurchaseContext): Promise<BuyerResult> {
    const status = await this.tools.call("get_purchase_status", {
      evaluation_id: context.evaluationId,
    });
    const state = requiredString(status, "state");
    const reasonCode = requiredString(status, "reason_code");
    if (state === "evaluation_denied" && reasonCode === "QUOTE_EXPIRED") {
      return this.refreshExpiredQuote(context);
    }
    if (state === "human_approval_required") {
      return {
        state,
        approvalUrl: requiredString(status, "approval_url"),
        context,
      };
    }
    if (state === "payment_failed") {
      return { state, reasonCode, context };
    }
    if (state === "refunded" || state === "compensation_pending" || state === "manual_review") {
      return { state, reasonCode, context };
    }
    if (state !== "access_ready" && state !== "fulfilled") {
      return {
        state: "waiting",
        reasonCode,
        ...(integerValue(status.retry_after_seconds)
          ? { retryAfterSeconds: integerValue(status.retry_after_seconds)! }
          : {}),
        context,
      };
    }
    const transactionId = requiredString(status, "transaction_id");
    const entitlement = await this.tools.call("get_entitlement", {
      transaction_id: transactionId,
    });
    const entitlementRecord = requiredRecord(entitlement, "entitlement");
    const entitlementId = requiredString(entitlementRecord, "entitlement_id");
    const capability = await this.tools.call("request_capability", {
      entitlement_id: entitlementId,
    });
    const executed = await this.tools.call("execute_paid_resource", {
      merchant_slug: context.merchantSlug,
      service_slug: context.serviceSlug,
      input: context.input,
      capability: requiredString(capability, "token"),
    });
    if (executed.state === "executing") {
      return { state: "waiting", reasonCode: requiredString(executed, "reason_code"), context };
    }
    return {
      state: "fulfilled",
      result: executed.result,
      resultHash: requiredString(executed, "result_hash"),
      replayedResult: executed.replayed_result === true,
      context,
    };
  }

  private async refreshExpiredQuote(context: PurchaseContext): Promise<BuyerResult> {
    const quote = await this.tools.call("request_quote", {
      service_id: context.serviceId,
      input: context.input,
    });
    const quoteId = requiredString(quote, "id");
    const evaluated = await this.tools.call("evaluate_quote", {
      policy_id: context.policyId,
      quote_id: quoteId,
    });
    const evaluation = requiredRecord(evaluated, "evaluation");
    const refreshed: PurchaseContext = {
      ...context,
      quoteId,
      evaluationId: requiredString(evaluation, "id"),
    };
    if (evaluated.state === "denied") {
      const reasonCodes = Array.isArray(evaluation.reason_codes)
        ? evaluation.reason_codes.filter((value): value is string => typeof value === "string")
        : ["POLICY_DENIED"];
      return { state: "denied", reasonCodes, context: refreshed };
    }
    return {
      state: "human_approval_required",
      approvalUrl: requiredString(evaluated, "approval_url"),
      context: refreshed,
    };
  }
}

export class KeywordBuyerPlanner implements BuyerPlanner {
  proposeService(intent: BuyerIntent, catalog: Record<string, unknown>): ServiceProposal {
    const merchants = Array.isArray(catalog.merchants) ? catalog.merchants : [];
    const candidates: Array<ServiceProposal & { text: string }> = [];
    for (const merchantValue of merchants) {
      if (!isRecord(merchantValue) || !Array.isArray(merchantValue.services)) continue;
      for (const serviceValue of merchantValue.services) {
        if (!isRecord(serviceValue)) continue;
        candidates.push({
          merchantId: requiredString(merchantValue, "id"),
          merchantSlug: requiredString(merchantValue, "slug"),
          serviceId: requiredString(serviceValue, "id"),
          serviceSlug: requiredString(serviceValue, "slug"),
          text: `${String(serviceValue.name ?? "")} ${String(serviceValue.description ?? "")} ${String(serviceValue.slug ?? "")}`.toLowerCase(),
        });
      }
    }
    const words = intent.request
      .toLowerCase()
      .split(/[^a-z0-9]+/)
      .filter((word) => word.length >= 4 && !["with", "only", "more", "than"].includes(word));
    const ranked = candidates
      .map((candidate) => ({
        candidate,
        score: words.reduce((score, word) => score + (candidate.text.includes(word) ? 1 : 0), 0),
      }))
      .sort((left, right) => right.score - left.score);
    const selected = ranked[0];
    if (!selected || selected.score === 0) {
      throw new Error("No catalog service matched the requested intent");
    }
    const { text: _text, ...proposal } = selected.candidate;
    return proposal;
  }
}

export function parseOrbitalIntent(request: string): BuyerIntent {
  const norad = /\bNORAD\s+(\d{1,8})\b/i.exec(request)?.[1];
  const rupees = /(?:₹|INR\s*)(\d+(?:\.\d{1,2})?)/i.exec(request)?.[1];
  if (!norad || !rupees || !/one[- ]time/i.test(request)) {
    throw new Error("Intent must include NORAD ID, maximum INR amount, and one-time only");
  }
  const maximumAmount = Math.round(Number.parseFloat(rupees) * 100);
  if (!Number.isSafeInteger(maximumAmount) || maximumAmount < 0) {
    throw new Error("Intent maximum amount is invalid");
  }
  return {
    request,
    maximumAmount,
    currency: "INR",
    purchaseType: "one_time",
    input: { norad_id: Number.parseInt(norad, 10) },
  };
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function requiredRecord(record: Record<string, unknown>, key: string): Record<string, unknown> {
  const value = record[key];
  if (!isRecord(value)) throw new Error(`Tool response is missing ${key}`);
  return value;
}

function requiredString(record: Record<string, unknown>, key: string): string {
  const value = record[key];
  if (typeof value !== "string" || value.length === 0) {
    throw new Error(`Tool response is missing ${key}`);
  }
  return value;
}

function integerValue(value: unknown): number | undefined {
  return typeof value === "number" && Number.isSafeInteger(value) && value > 0
    ? value
    : undefined;
}
