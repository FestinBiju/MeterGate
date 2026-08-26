# Payment reconciliation required

**Symptom:** A payment transaction is `reconciliation_required`.

**Meaning:** Locally observed payment evidence is contradictory, incomplete, mismatched, or indicates multiple captures.

**Safe checks:** Review the immutable payment events and exact local order/payment bindings. Do not accept client-supplied provider truth.

**Allowed operator action:** Run the bounded payment reconciliation action, which fetches the exact Razorpay Order and Payments and compares receipt, amount, currency, capture status, and uniqueness.

**Resolution criteria:** Authoritative provider evidence is internally consistent and the transaction leaves `reconciliation_required` through the payment state machine.

**Verification:** Verify the provider-fetch event, immutable operator action, incident resolution eligibility, and derived gate state.

**Forbidden:** Never manually set `paid`, edit attempts, or issue an entitlement.
