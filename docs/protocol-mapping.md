# Protocol mapping

As of 3 September 2026. Nothing here claims conformance; it says which vocabulary and which design this borrows.

## NPCI Unified Agent Protocol (UAP): reported design, no public spec
Reported in press coverage (Business Standard, 8 Jul 2026; Reuters, 1 Sep 2026): a trust layer where AI agents are registered, verified and authorized to transact on UPI rails, layered on UPI Circle (delegation) and Reserve Pay (fund blocking), with user-set spending limits, audit trails and a central agent repository. Expected to be unveiled at Global Fintech Fest, 8–11 Sept 2026. No public specification or sandbox exists as of the date above; this project models the reported design and does not claim conformance.

| UAP (reported) | Here |
|---|---|
| Register / verify / authorize agents | `registry.py` + R01–R03; `revoke()` is permanent |
| User-set rule-based limits | Intent Mandate caps, allow-list, categories, currency, expiry |
| Audit trail | hash-chained ledger with per-decision rule trails |
| Delegation (UPI Circle) | user → agent Intent Mandate; cap breaches need a user-signed step-up |

Caveat on the registry: it is an in-process, unsigned dict seeded by the orchestrator, so whoever runs the orchestrator is the root of trust for agent identity; UAP's reported design centralises that in an NPCI-operated repository.

## Google AP2 (Agent Payments Protocol)
Three signed mandates, Intent, Cart and Payment, as verifiable credentials forming a chain. Here: the same three objects plus an Agent Proposal (so the merchant's cart can be checked against what the agent asked for) and a Step-Up Token, signed as Ed25519 envelopes over canonical JSON rather than W3C VCs. The gate's chain rules (R06, R08, R11) are what make the chain a chain.

## OpenAI/Stripe ACP (Agentic Commerce Protocol)
ACP-inspired field names (`item_id`, `title`, `description`, `url`, price, availability, `image_url`), not the ACP product-feed schema; the `.well-known/agent-commerce.json` file is our own discovery convention. Here: `merchant_data/feed.json` and `merchant_data/.well-known/agent-commerce.json`. No ACP checkout API or shared payment tokens; the merchant answers a proposal with a signed, price-locked Cart Mandate instead.

## MCP
Razorpay ships an official MCP server and its Agent Studio is built on an agent SDK. Here the agent's two tools (`browse_catalog`, `propose_cart`) are plain function-calling tools over an OpenAI-compatible chat-completions API; exposing the merchant as an MCP server is listed as future work. Deliberately, the agent is not given the Razorpay MCP server: that would put the LLM in the trust path, which is the one thing this design refuses to do.

## x402
HTTP 402 machine payments in stablecoins. Landscape only; off-rails for INR / Razorpay.
