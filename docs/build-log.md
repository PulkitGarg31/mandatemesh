# Build log

Real obstacles hit while building, and how each was solved. Feeds the form question
"Build Challenges & Technical Obstacles". Newest at the bottom. Keep entries honest and short.

## 2026-09-03

- Research doc recommended "submit the form now, polish later". The form requires the repo URL and
  video link and is marked final-on-submit, so the plan was reversed: build first, submit once.
- The 2-week plan had to fit ~16 hours: cut to one merchant, no UI, no webhooks, polling instead.
- No paid LLM credits: switched the agent to an OpenAI-compatible client so Gemini's free tier and a
  local Ollama model both work through one code path. The gate does not care which model proposes.
- Razorpay test mode allows only 30 Payment Links per account, so all development and tests run on
  a fake executor; real calls are reserved for the smoke test and the recorded runs.

## 2026-09-03 (build)

- Razorpay's Payment Link docs say the `payments` array is populated only after a payment is captured,
  so a `failure@razorpay` attempt would never show up there. Caught in review before any real call was
  made; the executor now reads attempts from `GET /orders/{id}/payments` once the link carries an
  `order_id`, and from the Payments list matched on `notes.payment_id` before that.
- The plan assumed openai 1.x; 3.7.0 was installed. Tool calls are a union of function and custom calls
  (filtered to `type == "function"`), Ollama's compatibility layer rejects `tool_choice` (none is sent;
  `auto` is the default), and Gemini 3 returns thought signatures in `extra_content` that must be echoed
  back on the assistant turn or the next request fails.
- Groq retired `llama-3.3-70b-versatile` in August 2026; `.env.example` and the docs now point at
  `openai/gpt-oss-120b`.
- The first `crypto.py` signed only the payload, so an envelope could be relabelled with a different
  `signer` (or `alg`) and still verify. The signature now covers canonical JSON of `{alg, payload, signer}`;
  base64url decoding is strict and NaN is rejected.
- Dataclasses do not type-check, so a payload with a string amount or an extra key crashed the gate with a
  `TypeError` instead of a decision. `from_payload` now parses strictly (exact keys, scalar types, nested
  items) and raises `MalformedMandate`; the gate reports it as rule R00.
- A cart total of 10**400 crashed the gate: the money formatter went through `float`. Formatting is
  integer-only now, and `evaluate` wraps `_evaluate` so any internal error is a DENY on R99 with the
  exception type in the trail. The gate never raises.
- A colluding merchant could sign a cart with a negative-quantity line whose total still equalled the sum
  of lines. R10 now requires at least one line, `qty >= 1`, `unit_price_paise >= 0` and a positive total;
  R08 also checks proposal->intent and proposal->merchant.
- The ledger receipt selected related events by substring match, so `pm_1` also picked up `pm_10`. It now
  matches whole `payment_id` / `cart_id` / `intent_id` fields and selects the capture by `payment_id`.
- After two failed attempts the orchestrator recorded `razorpay.link.cancelled` even when the cancel call
  failed, which is exactly what happens when the customer pays at that moment. A failed cancel now triggers
  one final poll: a late capture is recorded as `payment.captured`, otherwise `razorpay.link.cancel_failed`.
- Rich interpreted the orchestrator's `[gate]` / `[razorpay]` prefixes as markup tags and swallowed them.
  Progress lines print with markup disabled, and LLM text (justification, errors) is escaped before it
  reaches a Rich table.
- Final review: a hand-edited feed with a float price crashed the run; feed is now validated at load and a
  malformed signed cart becomes a recorded quote rejection; receipt now states chain status.
- Smoke test on a real test-mode link (plink_TXXr4OVtoqtI72): UPI is not enabled on this test account, so the checkout offers Cards, Netbanking and Wallet; the Netbanking mock bank page gives Success/Failure buttons. The failed attempt (pay_TXXv8QpFsEHJBo, BAD_REQUEST_ERROR "declined by the bank") appeared only via the Payments API and carried both the link order_id (order_TXXrrD9u8G8WJS) and the link notes.payment_id, so the executor primary (order) and fallback (notes) lookups both work; the captured attempt appeared in the link own payments array. Docs and prompts switched from "pay with UPI ids" to "mock bank Success/Failure".
  (`order_id` on the payment entity, `notes.payment_id`, or both). No real Razorpay call has been made yet;
  every ledger under `runs/` so far is a fake-executor run.
- Live runs on real test-mode links (scripted agent): happy -> plink_TXXxO8zlFMME0c captured as pay_TXXz14FiKBhGB7 (INR 910, 8 events, chain verified, receipt written); payfail -> first take captured directly because the Failure click was skipped (Razorpay listed a single captured payment); second take plink for run live-payfail-2 -> attempt 1 failed pay_TXY291SZnDR6z4, gate re-authorised, attempt 2 captured pay_TXY2R4EJYnYL73 (11 events: payment.failed, gate.decision, payment.retry, payment.captured; chain verified). Test links used so far: 4 of 30.
- Local models via Ollama on a CPU-only laptop, fake executor: llama3.2 (3B) called propose_cart with an empty item list -> merchant rejected "empty cart" (outcome quote_rejected, nothing created); mistral (7B) first timed out at the 60 s client timeout (outcome no_proposal), and with LLM_TIMEOUT_S=300 it skipped browse_catalog and invented SKUs (rice-bag, dal-pack) -> "unknown sku" (quote_rejected). Each turn took 15-25 s. Both runs failed closed with a clean ledger, which is the point of the design, but a capable hosted model (NVIDIA NIM llama-3.3-70b or Gemini) is needed for a clean agent-driven happy path. Added LLM_TIMEOUT_S.
- NVIDIA NIM (free developer key): meta/llama-3.3-70b-instruct returned HTTP 410 (end of life 2026-08-26); nvidia/llama-3.1-nemotron-70b-instruct and mistralai/mistral-large-2-instruct are listed by /v1/models but return 404 on the free endpoint; nvidia/nemotron-3-super-120b-a12b worked first time: browse_catalog, then propose_cart with exactly the staples basket, gate ALLOW, captured (fake executor). Docs and .env.example now name that model.
- Model behaviour with nemotron-3-super on the fake executor: stepup request -> the model dropped one rice bag to fit the INR 1,500 per-transaction cap (cart INR 1,350, ALLOW, paid), and poison request -> it ignored the injected "add 50 units" text and proposed 2 units of ghee (INR 1,200, ALLOW). So a well-behaved model turns both failure scenarios into happy paths, which is why the video records stepup and poison with --agent scripted; the gate, not the prompt, is the control.

## 2026-09-03 (Part II: delegation, refunds, replay)

Constraints carried over from Part I, because they shaped Part II too: the Payment Link `payments`
array is populated only after capture (so failed attempts are still read from the link's order); the
free NIM endpoint 404s or 410s on most listed models, which is why one working model is pinned rather
than a menu; and small local models fail closed (empty carts, invented SKUs) rather than misbehaving,
which is the design working but not a demo. Everything below is new to Part II.

- Refunds needed the real API shape before the rules could be written. Verified once in test mode:
  `client.payment.refund(payment_id, {"amount", "notes"})` returned `rfnd_TXZlazlLHEtbmo` with status
  `pending` (not `processed`) and the payment's `amount_refunded` updated. So `RefundInfo` carries the
  status verbatim instead of asserting one, and the fake executor mirrors the same three fields.
- The hard question in the refund design was who gets to name the amount. Taking the merchant's
  `refund_paise` at face value would make the merchant the authority on how much of the user's money comes
  back, which is the same mistake as trusting the model. RF06 recomputes the amount from the signed cart's
  own unit prices and refuses the attestation unless the claim matches exactly; the merchant states which
  lines were short, and nothing else.
- Review found a trail gap in the delegation rules: when R19 denied, the chain walk had already done the
  R18 work but no R18 check was recorded, so the trail showed a subset failure with no evidence that the
  chain itself was sound. The walk was split into two passes -- shape (R18) recorded first, subset (R19)
  second -- which also made the details easier to read.
- Same review pass, two abuses the rules did not yet stop: the same `sub_id` repeated to make one grant
  look like a chain of fresh authority, and an arbitrarily long chain as a way to make the gate work.
  R18 now refuses a repeated id (the root intent id counts as seen) and caps the chain at 8 links.
- Cap breaches under delegation were reported against the leaf, which read as if only the junior agent
  had over-reached. Since R19 makes caps non-increasing down the chain, the failing detail now names the
  root-most breaching link, and the passing detail names the tightest cap.
- Writing `Ledger.replay()` was mostly discovering how much a decision event does *not* say. Six things
  had to be worked around or fixed:
  1. `gate.decision` records `cart_id` but no `proposal_id`, so the proposal is reachable only through
     the cart's `proposal_id` -- an extra hop that has to happen in the right order.
  2. `chain_ids[0]` is the root intent, not a sub-mandate, so the reconstruction has to skip it; feeding
     it to the `mandate.sub.created` lookup fails on a perfectly good ledger.
  3. Public keys ride on events that are about something else: `user_pubkey` on `mandate.intent.created`,
     `merchant_pubkey` on `merchant.cart.quoted`, `gate_pubkey` on `refund.decision` itself. Replay reads
     each from its host event and reports a missing one instead of assuming a key.
  4. The live registry makes revocation permanent: `register()` raises on a revoked id. A rebuild that
     just replays every `agent.registered` therefore either raises on such a ledger or, if the error is
     swallowed, ends up more permissive than the registry it is imitating. The rebuild skips a
     registration for a revoked id, and guards `revoke` on an id it never saw registered.
  5. The decision payload mixes the verdict with the bookkeeping (`**d.to_dict()` next to `now`,
     `spent_paise`, `chain_ids`), so comparison has to be restricted to the four decision fields rather
     than to the whole payload.
  6. The gate must not import the ledger, and the ledger must import the gate to replay anything. The
     import inside `replay()` is load-bearing, not a style choice, and is commented as such.
- Replay is deliberately total: a missing envelope, a missing key or any other surprise becomes a row
  saying the decision could not be replayed, never an exception and never a silent pass. An audit tool
  that crashes on a hostile file has told the attacker what to send.
- The point of the feature, confirmed by hand on a doctored run: flip a recorded verdict from ALLOW to
  DENY and re-hash every line from there on, and `ledger verify` says "verified" while `ledger replay`
  reports `recorded 'DENY', replayed 'ALLOW'` and exits 2.
