# Decision records

## D1. The LLM is outside the trust path
The model can browse and propose; it cannot sign anything but its own proposal and cannot reach Razorpay. The agent module is constructed with the agent key only, and the LLM never receives key material. Reason: prompt injection through catalogs is unsolved; a control that depends on the model behaving is not a control. Consequence: the model is interchangeable (Gemini free tier, Ollama, Groq through one `openai`-client code path) and the demo needs no paid API.

## D2. A pure-function gate holds the policy
`PolicyGate.evaluate(GateInput) -> Decision` with every input explicit: envelopes, public keys, prior spend, `now`. Reason: one unit test per rule, replayable from the ledger, no hidden state, and the model cannot influence it except through the one signed proposal. Consequence: the orchestrator computes prior spend from the ledger and passes it in, and the orchestrator (not the gate) signs the Payment Mandate after ALLOW.

## D3. Signed mandates instead of a shared database
Intent, Proposal, Cart, Step-Up and Payment are Ed25519-signed envelopes over canonical JSON; the signature covers `alg`, `signer` and `payload` together. Reason: each party's authority is verifiable without trusting the process that carries it; this mirrors AP2's intent/cart/payment chain. Consequence: JWS-like, not W3C Verifiable Credentials, a stated limitation.

## D4. Hash-chained JSONL ledger, not a database
Reason: one file per run, human-readable, tamper-evident with `sha256(prev_hash + canonical(event))`, no dependencies. Consequence: no concurrent writers, fine for one process; the chain does not detect tail truncation or a re-hashed last line, so the receipt's head hash is the out-of-band anchor.

## D5. Polling instead of webhooks
Reason: webhooks need a public URL. The executor polls the Payment Link every 3 s for capture and, because the link's `payments` array lists only captured payments, reads failed attempts from the link's order (see D10). Consequence: capture and failed attempts are both detected within seconds; `--poll-timeout` bounds each attempt (default 180 s), a timeout counts as a failed attempt, and every SDK call has a 10 s timeout with tolerance for a few transient errors.

## D6. Step-up as a signed token bound to one cart
Reason: "ask the human" must be as unforgeable as the mandate itself. The token names the intent id, the cart id and the approved amount (the exact cart total shown in the prompt) and expires in 10 minutes. A valid token covers both caps for that one cart; an invalid one is a DENY on R16.

## D7. Fake executor for everything but the demo
Reason: 30 Payment Links per test account. All tests and development runs use `FakeExecutor` with the same interface; real calls happen only in the smoke test and the recorded runs. `RazorpayExecutor` refuses any key that does not start with `rzp_test_`.

## D8. The gate never raises (R99)
`evaluate` wraps `_evaluate` and turns any exception into a DENY on `R99_GATE_ERROR` with the exception type in the trail. Reason: found in review when a 10^400 cart total crashed the money formatter; an exception inside a rule must never turn into an ALLOW by accident or a traceback on camera. Consequence: money formatting is integer-only and R99 is a guard, not a rule, so it never appears in a passing trail.

## D9. Strict parsing at the deserialization boundary (`MalformedMandate`)
Dataclasses do not type-check, so `from_payload` parses strictly: exact keys, `int`/`str`/`list` scalar types (bool is not int), nested items. Reason: a string amount or an extra key from a chatty model crashed the gate with a `TypeError` instead of producing a decision. Consequence: the gate's first rule, R00, reports a malformed payload as a DENY with a trail; the merchant rejects a malformed proposal with `MerchantError` and validates its feed at load (five required keys, integer non-negative `price_paise`); and a signed cart that fails strict parsing is recorded as `merchant.quote.rejected` rather than crashing the run.

## D10. Payments API via the link's order for failed attempts
Razorpay documents that a Payment Link's `payments` array is populated only after a payment is captured, so a `failure@razorpay` attempt never appears there. Once a customer attempts payment the link carries an `order_id`, and `GET /orders/{id}/payments` lists every attempt including failed ones; before that, the Payments list is matched on the payment-mandate id in the link's `notes`. Reason: the `payfail` scenario needs failed attempts within seconds and without webhooks. Consequence: the smoke script `scripts/smoke_razorpay.py` must confirm on a real attempt which field ties the failed payment to the link; that result is recorded in `docs/build-log.md`.

## D11. Honest link closing: never record `cancelled` unless the cancel succeeded
When a run ends without a capture the orchestrator cancels the link. If Razorpay refuses (typically because the customer paid at that moment), one final poll looks for a late capture and records `payment.captured`; otherwise `razorpay.link.cancel_failed` is recorded. `razorpay.link.cancelled` is written only after a successful cancel. Reason: the ledger must never say "nothing charged" when money moved. Consequence: `payment.abandoned` carries the real attempt count and, when the gate refused the retry, the rule id; Ctrl+C during polling also closes the link before exiting with 130.
