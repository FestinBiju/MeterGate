# Razorpay Test Mode Refund Acceptance — 2026-08-26

## Result

PASS. MeterGate executed one real Razorpay Test Mode refund and converged it to
trusted provider status `processed`. Razorpay Test Mode moved no real money.

The final provider verification was performed at
`2026-08-26T12:11:25.6757331Z` (`2026-08-26T17:41:25.6757331+05:30`). No API
keys, secrets, signatures, raw webhook bodies, capability tokens, or session
credentials are stored in this record.

## Immutable payment and fulfillment evidence

| Field | Value |
| --- | --- |
| Payment transaction | `txn_01M0YY8Y47N90W104XS78XE57Q` |
| Historical payment state | `paid` |
| Captured amount | `500` minor units (`INR`) |
| Razorpay Order | `order_TUO2Y7fVnEmxU0` (`paid`) |
| Razorpay Payment | `pay_TUO3G34Y0hQ07r` (`captured`) |
| Payment attempt | `pmt_01M0YYAZY2X5CMQHPHQK2XZ650` |
| Paid at | `2026-08-26T11:47:25.752050Z` |
| Entitlement | `ent_01M0YYB2BRXNKH51HATEW7TSBT` |
| Fulfillment execution | `ful_01M0YYC1FTSBKJPZ87KQ5HD2Z2` |
| Fulfillment state | `permanent_failure` |
| Failure code | `FULFILLMENT_ENTITLEMENT_EXPIRED` |
| Compensation required | `true` |

The entitlement expired without a delivered result. Expiry finalization moved
the existing fulfillment aggregate to permanent failure and staged compensation;
it did not rewrite the captured payment.

## Compensation and refund evidence

| Field | Value |
| --- | --- |
| Compensation case | `cmp_01M0YYXD6PXB8Z0BYYN0DC62A2` |
| Decision provenance | `automatic_approved` |
| Decision reason | `COMPENSATION_FULL_REFUND_FULFILLMENT_FAILED` |
| Approved amount | `500` minor units (`INR`) |
| Final decision state | `completed` |
| Local refund | `rfd_01M0YZESF1ZJ0PRBTGCNSNAAQ8` |
| Stable provider receipt | `rfd_01M0YZESF1ZJ0PRBTGCNSNAAQ8` |
| Razorpay refund | `rfnd_TUOOPHp7us13H4` |
| Provider payment binding | `pay_TUO3G34Y0hQ07r` |
| Refund amount | `500` minor units (`INR`) |
| Local refund state | `refunded` |
| Provider status | `processed` |
| Provider created at | `2026-08-26T12:06:58Z` |
| Local request started at | `2026-08-26T12:06:59.271872Z` |
| Local completion at | `2026-08-26T12:07:17.545362Z` |
| Commerce outcome | `refunded` |
| Reconciliation overlay | none |

The refund outbox completed after three worker cycles. The first provider
response was `pending`; a later authoritative provider API read returned
`processed`, completing the refund and compensation case. No refund webhook was
received during the acceptance window, so convergence evidence came from the
supported API reconciliation path.

## Fresh provider read

The final server-side Razorpay SDK read returned this validated subset:

```json
{
  "provider_refund_id": "rfnd_TUOOPHp7us13H4",
  "provider_payment_id": "pay_TUO3G34Y0hQ07r",
  "amount": 500,
  "currency": "INR",
  "receipt": "rfd_01M0YZESF1ZJ0PRBTGCNSNAAQ8",
  "status": "processed",
  "created_at": "2026-08-26T12:06:58+00:00"
}
```

## Ordered compensation audit

| Sequence | Event | Actor | Reason | Occurred at (UTC) |
| ---: | --- | --- | --- | --- |
| 1 | `compensation_case_created` | `system` | `COMPENSATION_CASE_CREATED` | `2026-08-26T11:57:29.142866Z` |
| 2 | `compensation_recommended` | `system` | `COMPENSATION_FULL_REFUND_FULFILLMENT_FAILED` | `2026-08-26T11:57:29.142866Z` |
| 3 | `compensation_approved` | `system` | `COMPENSATION_FULL_REFUND_FULFILLMENT_FAILED` | `2026-08-26T11:57:29.142866Z` |
| 4 | `refund_outbox_created` | `system` | `REFUND_REQUESTED` | `2026-08-26T11:57:29.142866Z` |
| 5 | `refund_requested` | `refund_worker` | `REFUND_REQUESTED` | `2026-08-26T12:06:58.785831Z` |
| 6 | `refund_request_started` | `refund_worker` | `REFUND_REQUEST_STARTED` | `2026-08-26T12:06:59.271872Z` |
| 7 | `razorpay_refund_created` | `provider_api` | `RAZORPAY_REFUND_CREATED` | `2026-08-26T12:07:01.425315Z` |
| 8 | `refund_processing` | `provider_api` | `REFUND_PROCESSING` | `2026-08-26T12:07:01.425315Z` |
| 9 | `refund_completed` | `provider_api` | `REFUND_COMPLETED` | `2026-08-26T12:07:17.545362Z` |
| 10 | `compensation_closed` | `provider_api` | `COMPENSATION_CLOSED_REFUNDED` | `2026-08-26T12:07:17.545362Z` |

## Acceptance assertions

- The payment remained historically `paid`.
- Refund authority and amount were server-derived from the approved case.
- The durable local refund ID was reused as the provider receipt.
- Razorpay returned an actual `rfnd_…` Test Mode identifier.
- Local completion waited for fresh provider status `processed`.
- The completed case projects `commerce_outcome=refunded`.
- Compensation remains related evidence; no entitlement or payment history was
  mutated to fabricate refund semantics.

