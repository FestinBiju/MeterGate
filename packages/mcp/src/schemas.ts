import * as z from "zod/v4";

const id = (prefix: string) =>
  z.string().regex(new RegExp(`^${prefix}_[0-7][0-9A-HJKMNP-TV-Z]{25}$`));

export type JsonValue =
  | null
  | boolean
  | number
  | string
  | JsonValue[]
  | { [key: string]: JsonValue };

const jsonValueBase: z.ZodType<JsonValue> = z.lazy(() =>
  z.union([
    z.null(),
    z.boolean(),
    z.number().finite().safe(),
    z.string().max(262_144),
    z.array(jsonValueBase).max(10_000),
    z.record(z.string().max(200), jsonValueBase),
  ]),
);

export const boundedJson = jsonValueBase.superRefine((value, context) => {
  const encoded = new TextEncoder().encode(JSON.stringify(value));
  if (encoded.byteLength > 262_144) {
    context.addIssue({ code: "custom", message: "Input exceeds 256 KiB" });
  }
  const pending: Array<[JsonValue, number]> = [[value, 0]];
  let nodes = 0;
  while (pending.length > 0) {
    const [item, depth] = pending.pop()!;
    nodes += 1;
    if (nodes > 10_000 || depth > 32) {
      context.addIssue({
        code: "custom",
        message: "Input nesting or item count exceeds the safe limit",
      });
      return;
    }
    if (Array.isArray(item)) {
      pending.push(...item.map((child): [JsonValue, number] => [child, depth + 1]));
    } else if (item !== null && typeof item === "object") {
      pending.push(
        ...Object.values(item).map(
          (child): [JsonValue, number] => [child, depth + 1],
        ),
      );
    }
  }
});

export const listServicesInput = z.object({}).strict();
export const getServiceInput = z.object({ service_id: id("svc") }).strict();
export const resourceInput = z
  .object({
    merchant_slug: z.string().regex(/^[a-z0-9]+(?:-[a-z0-9]+)*$/).max(100),
    service_slug: z.string().regex(/^[a-z0-9]+(?:-[a-z0-9]+)*$/).max(100),
    input: boundedJson,
  })
  .strict();
export const quoteInput = z
  .object({ service_id: id("svc"), input: boundedJson })
  .strict();
export const policyInput = z
  .object({
    maximum_amount: z
      .number()
      .int()
      .safe()
      .nonnegative()
      .describe(
        "Maximum spend in the currency's integer minor unit. INR uses paise: ₹10.00 is 1000, and 500 is ₹5.00.",
      ),
    allowed_currencies: z.array(z.string().regex(/^[A-Z]{3}$/)).min(1).max(100).optional(),
    allowed_merchant_ids: z.array(id("mrc")).min(1).max(100).optional(),
    allowed_service_ids: z.array(id("svc")).min(1).max(100).optional(),
    allowed_service_types: z
      .array(z.enum(["api", "dataset", "report", "digital_service"]))
      .min(1)
      .max(100)
      .optional(),
    allowed_purchase_types: z.tuple([z.literal("one_time")]),
    expires_in_seconds: z.number().int().min(1).max(86_400),
  })
  .strict();
export const evaluationInput = z
  .object({ policy_id: id("pol"), quote_id: id("qte") })
  .strict();
export const purchaseStatusInput = z.object({ evaluation_id: id("pye") }).strict();
export const entitlementInput = z.object({ transaction_id: id("txn") }).strict();
export const capabilityInput = z.object({ entitlement_id: id("ent") }).strict();
export const executeInput = resourceInput
  .extend({ capability: z.string().min(1).max(4096) })
  .strict();
