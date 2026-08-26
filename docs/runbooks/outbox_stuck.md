# Outbox stuck

**Symptom:** An entitlement or refund outbox item remains unprocessed beyond the configured SLA.

**Meaning:** Durable work exists, but the owning worker has not completed its fenced effect.

**Safe checks:** Check worker heartbeat, lease generation, availability time, attempt count, last safe error, and authoritative aggregate.

**Allowed operator action:** Restore or restart the owning worker and allow its normal fenced retry path to run.

**Resolution criteria:** The exact outbox item has `processed_at`, or a bounded scheduled retry is current and the item is no longer beyond SLA.

**Verification:** Confirm the stuck count and deterministic alert clear while the corresponding domain event appears.

**Forbidden:** Never delete outbox work, edit its payload, reset attempts, or run arbitrary SQL.
