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
- (pending) smoke test result: which field tied the failed payment to the link
  (`order_id` on the payment entity, `notes.payment_id`, or both). No real Razorpay call has been made yet;
  every ledger under `runs/` so far is a fake-executor run.
