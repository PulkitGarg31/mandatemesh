# Architecture

## Components and what each may touch

| Component | May read | Signs with | May call |
|---|---|---|---|
| Buyer agent (`agent.py`) | mandate summary (caps, merchants, categories), the user's request, the catalog as untrusted JSON | agent key (Agent Proposal only) | the LLM endpoint only (`LLMAgent`); nothing (`ScriptedAgent`) |
| Mock merchant (`merchant.py`) | its own feed (validated at load: five required keys, integer non-negative `price_paise`), the signed proposal | merchant key (Cart Mandate) | nothing |
| Registry (`registry.py`) | agent id → public key, status | — | nothing |
| Policy gate (`gate.py`) | the intent, proposal and cart envelopes, an optional step-up envelope, the user public key, the merchant public keys, prior spend, `now` | — (pure function; returns a `Decision`) | nothing: no I/O, no clock, no LLM, no network |
| Executor (`executor.py`) | a signed Payment Mandate | — | Razorpay: the only module that imports `razorpay` and the only holder of the Razorpay credentials |
| Ledger (`ledger.py`) | events | — | the local JSONL file |
| Orchestrator (`orchestrator.py`) | everything above | user key (Intent Mandate, Step-Up Token), gate key (Payment Mandate, only after `Decision.verdict == "ALLOW"`) | wires the others; writes every ledger event |

Trust boundary: the agent module is constructed with the agent key only; the LLM never receives key material. All four Ed25519 keys are loaded by the one orchestrator process in this demo (a production deployment would load each role's key in its own process). The gate never calls the LLM.

## Sequence (happy path)

```mermaid
sequenceDiagram
    participant U as User
    participant O as Orchestrator
    participant A as Agent (LLM)
    participant M as Merchant
    participant G as Gate
    participant X as Executor
    participant R as Razorpay
    participant L as Ledger
    O->>L: mandate.intent.created (user-signed Intent Mandate)
    O->>L: agent.registered
    O->>A: propose(intent summary, request)
    A->>M: browse_catalog
    M-->>A: feed as untrusted JSON
    A-->>O: signed Agent Proposal
    O->>L: agent.proposal
    O->>M: quote(proposal)
    M-->>O: signed, price-locked Cart Mandate
    O->>L: merchant.cart.quoted
    O->>G: evaluate(intent, proposal, cart, prior spend, now)
    G-->>O: Decision ALLOW with a 17-check trail
    O->>L: gate.decision
    O->>L: mandate.payment.created (gate-signed Payment Mandate)
    O->>X: create_payment_link(Payment Mandate)
    X->>R: create Payment Link (notes = mandate ids)
    O->>L: razorpay.link.created
    U->>R: pays the link (success@razorpay)
    X->>R: poll the link, then the order's payments
    X-->>O: paid
    O->>L: payment.captured
```

A merchant refusal, or a signed cart that fails strict parsing, is recorded as `merchant.quote.rejected` and the run stops with outcome `quote_rejected`; nothing is evaluated. Before the gate runs, the orchestrator checks the ledger for a `payment.captured` event with the same cart id and refuses a replay (`orchestrator.replay_refused`). Prior spend is the sum of `payment.captured` amounts for the intent, computed from the ledger and passed into the gate.

## Why the gate is a pure function

`PolicyGate.evaluate(GateInput) -> Decision` takes every input explicitly: envelopes, public keys, prior spend, the clock. No I/O, no globals, no LLM. That is what makes it testable one rule at a time (`tests/test_gate.py`), replayable from the ledger, and impossible for the model to influence except through the one signed proposal it is allowed to make.

Two guards keep it total. Parsing at the deserialization boundary is strict (`mandates.MalformedMandate`), so a payload with the wrong shape is rule R00, not a `TypeError`. `evaluate` wraps `_evaluate`, so any other exception becomes a DENY on R99 with the exception type in the trail. The gate never raises. Money is integer paise throughout, including the formatter in the check details.

## Where money moves

Exactly one call creates a money action: `RazorpayExecutor.create_payment_link(PaymentMandate, description, notes)`. The executor only accepts a `PaymentMandate`, and only the orchestrator constructs one, only after the gate returned ALLOW. The link carries the intent, cart, payment mandate and agent ids in its `notes`, and `reference_id` is the payment mandate id.

After that the executor polls: `payment_link.fetch` for `status == "paid"` (capture), and, because the link's own `payments` array lists only captured payments, `order.payments(order_id)` for failed attempts once the link carries an `order_id`, with `payment.all` matched on `notes.payment_id` as the fallback before that. A retry re-runs the gate on the same cart before the second attempt. When the run ends without a capture the link is cancelled; if the cancel fails, one last poll looks for a late capture before `razorpay.link.cancel_failed` is recorded.

## Ledger

`runs/<run-id>/ledger.jsonl`, one event per line: `{seq, id, ts, type, actor, payload, prev_hash, hash}` with `hash = sha256(prev_hash + canonical(event without hash))`. Payloads are normalised through JSON before hashing so what is hashed is exactly what a reload produces. `verify()` recomputes every hash and reports the first bad position (a hash mismatch, a `seq` out of place, or a line that does not parse). The chain detects modification, insertion, deletion and reordering; it does not detect tail truncation or a re-hashed last line, so the receipt's head hash is the out-of-band anchor. Tamper-evident, not tamper-proof. The receipt re-verifies the chain it is exported from and prints `- Chain: verified` or `- Chain: BROKEN at seq N` next to the head hash.
