# Fulfillment permanent failure

**Symptom:** A fulfillment is `permanent_failure` with `compensation_required`.

**Meaning:** The one logical merchant execution exhausted legal attempts or failed permanently; payment remains historical evidence.

**Safe checks:** Confirm no result was stored or released, the immutable quote refund policy, exact entitlement/execution binding, failure code, and compensation case.

**Allowed operator action:** Approve only a `manual_review` case with confirmed non-delivery; reject only with evidence of delivery or duplication.

**Resolution criteria:** The compensation is rejected with evidence, or the exact approved refund reaches a terminal provider-backed outcome.

**Verification:** Verify the immutable operator decision and resulting refund outbox, completed refund, or rejection in the unified timeline.

**Forbidden:** Never force success, change the quote, or bypass compensation quarantine.
