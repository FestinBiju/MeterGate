# Milestone 9 Operator Acceptance — 2026-08-26

## Scope and safety

This record preserves the physical local acceptance evidence for the MeterGate
Test Mode operator control plane. Razorpay Test Mode moved no real money. No API
keys, secrets, raw provider payloads, WebAuthn assertions, capability tokens,
CSRF tokens, session identifiers, or biometric data are stored here.

## Operator identity

| Field | Evidence |
| --- | --- |
| Authentication | Physical passkey ceremony completed through Windows Hello |
| Account | `acct_01M0Y1QF5W0NBCTRBJ7FE83HHS` |
| Operator role | `operator`, active |
| Operator page | `http://localhost:3000/operator` |

The browser session was established by a real human passkey ceremony. No
WebAuthn result was mocked or synthesized.

## Real dashboard evidence

The operator dashboard loaded authoritative local database projections:

| Metric | Observed value |
| --- | ---: |
| Transactions | 6 |
| Paid transactions | 4 |
| Test GMV | INR 17.00 |
| Completed refunds | 2 |
| Open incidents | 0 |

All three live worker projections were `healthy` during the acceptance window:

- `razorpay_webhook`: backlog 0
- `entitlement`: backlog 0
- `refund`: backlog 0

Both entitlement and refund outbox projections showed zero pending and zero
stuck items.

## Historical paid and refunded case

The dashboard opened the real historical transaction
`txn_01M0YY8Y47N90W104XS78XE57Q` and displayed these authoritative records:

| Artifact | Identifier / state |
| --- | --- |
| Razorpay order | `order_TUO2Y7fVnEmxU0` |
| Captured payment | `pay_TUO3G34Y0hQ07r` |
| Fulfillment | `ful_01M0YYC1FTSBKJPZ87KQ5HD2Z2`, permanent failure |
| Local refund | `rfd_01M0YZESF1ZJ0PRBTGCNSNAAQ8`, refunded |
| Provider refund | `rfnd_TUOOPHp7us13H4`, processed |

The UI kept the captured payment as historical fact while projecting the final
commerce outcome through compensation and refund evidence.

## Unified timeline

The ordered timeline visibly included actual evidence for quote creation,
policy evaluation, authorization, payment capture, entitlement issuance,
fulfillment failure/retry, compensation, refund request, and refund completion.
No missing event was fabricated.

## Recent-passkey enforcement

The payment reconciliation confirmation was opened for the real pending
transaction `txn_01M0YZ3SMV9X1QK9D2DMAS7MHN`. Submission after the configured
operator reauthentication window returned HTTP 403 with recent-passkey
protection. This demonstrates that an authenticated operator session alone is
not enough for a high-risk action.

## Completed physical controls

The operator completed an in-session Windows Hello passkey ceremony at
approximately 20:13 IST. MeterGate displayed recent-passkey confirmation, and
the API recorded `PASSKEY_LOGIN_SUCCESS`, `SESSION_CREATED`, and
`PASSKEY_REAUTH_SUCCESS` for operator account
`acct_01M0Y1QF5W0NBCTRBJ7FE83HHS`.

After reauthentication, a safe payment reconciliation was submitted for the
real pending transaction `txn_01M0YZ3SMV9X1QK9D2DMAS7MHN` at approximately
20:15 IST:

| Field | Observed value |
| --- | --- |
| Razorpay order | `order_TUOI2dCV83JWcO` |
| Razorpay payment | `pay_TUOJPsUNa02JGU` |
| Provider attempt state | `failed` |
| MeterGate transaction state | `payment_pending` |
| Operator timeline event | `PAYMENT_RECONCILIATION_TRIGGERED` |
| Timeline reason | `OPERATOR_RECONCILIATION_TRIGGERED` |

The action correctly preserved the pending transaction because the provider
attempt remains failed. The action was not silently treated as success, and its
immutable operator evidence appeared in the unified timeline.

An isolated browser session was already authenticated as the normal buyer
account `acct_01M0Z67E98HFDJXVYAY24J784E` (`Festin`). Navigating that session to
`/operator` displayed the denial: "This account does not have an active operator
role or needs recent passkey authentication." No operator data or controls were
exposed.

These observations complete the physical Milestone 9 acceptance checks. The
result is **PASS**.

## Related refund evidence

The full real Razorpay Test Mode refund record remains in
`docs/evidence/2026-08-26-razorpay-test-mode-refund.md` and is not duplicated or
reinterpreted here.
