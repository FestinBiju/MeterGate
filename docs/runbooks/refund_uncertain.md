# Refund uncertain

**Symptom:** A refund remains `refund_uncertain` beyond the configured alert threshold.

**Meaning:** The provider may have accepted the stable `rfd_…` intent, but MeterGate lacks conclusive evidence.

**Safe checks:** Check its durable dispatch intent, exact provider receipt, amount, payment binding, attempt timeline, and last reconciliation evidence.

**Allowed operator action:** Trigger bounded refund reconciliation only.

**Resolution criteria:** The exact provider refund is bound, or authoritative evidence proves a same-identity retry safe and the state leaves uncertainty.

**Verification:** Confirm the persistent-uncertainty work item and incident clear only after authoritative state convergence.

**Forbidden:** Never create a second refund, invent a new idempotency identity, mark the payment unpaid, or directly set `refunded`.
