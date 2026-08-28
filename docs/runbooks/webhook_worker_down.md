# Webhook worker down

**Symptom:** The Razorpay webhook worker heartbeat is missing or stale.

**Meaning:** Signed ingress can remain queued, but asynchronous provider evidence is not being applied.

**Safe checks:** Confirm Redis/PostgreSQL health, queue depth, worker configuration, heartbeat age, and signed ingress evidence references.

**Allowed operator action:** Restart the normal webhook worker and let idempotent processing resume.

**Resolution criteria:** Heartbeats remain fresh across the configured window and queued signed events are processed.

**Verification:** Verify heartbeat freshness, declining backlog/provider lag, and unchanged raw evidence hashes.

**Forbidden:** Never replay unsigned bodies, delete webhook evidence, or bypass signature verification.
