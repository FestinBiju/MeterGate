# Refund reconciliation required

**Symptom:** A refund is `reconciliation_required` or has a reconciliation overlay.

**Meaning:** Local and provider refund evidence cannot yet be safely converged.

**Safe checks:** Compare the local `rfd_…`, provider payment, receipt, amount, currency, cumulative refunded total, dispatch intent, and signed event references.

**Allowed operator action:** Run bounded refund reconciliation through the existing provider-fetch service and escalate mismatches.

**Resolution criteria:** Trusted exact-identity evidence removes the overlay and yields a legal local refund state.

**Verification:** Verify the reconciliation event, immutable operator action, completed compensation case when processed, and incident resolution eligibility.

**Forbidden:** Never delete webhook/outbox evidence, create a replacement refund, or clear quarantine directly.
