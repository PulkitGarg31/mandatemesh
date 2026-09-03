# Protocol mapping

As of 3 September 2026. Nothing here claims conformance; it says which vocabulary and which design this borrows.

## NPCI Unified Agent Protocol (UAP): reported design, no public spec
Reported in press coverage (Business Standard, 8 Jul 2026; Reuters, 1 Sep 2026): a trust layer where AI agents are registered, verified and authorized to transact on UPI rails, layered on UPI Circle (delegation) and Reserve Pay (fund blocking), with user-set spending limits, audit trails and a central agent repository. Expected to be unveiled at Global Fintech Fest, 8–11 Sept 2026. No public specification or sandbox exists as of the date above; this project models the reported design and does not claim conformance.

| UAP (reported) | Here |
|---|---|
| Register / verify / authorize agents | `registry.py` + R01–R03; `revoke()` is permanent |
| User-set rule-based limits | Intent Mandate caps, allow-list, categories, currency, expiry |
| Audit trail | hash-chained ledger with per-decision rule trails |
| Delegation (UPI Circle) | user → agent Intent Mandate, plus agent → agent Sub-Mandates; cap breaches need a user-signed step-up |

Caveat on the registry: it is an in-process, unsigned dict seeded by the orchestrator, so whoever runs the orchestrator is the root of trust for agent identity; UAP's reported design centralises that in an NPCI-operated repository. That caveat now covers delegation too: a Sub-Mandate is only as trustworthy as the registry entry for the agent that signed it.

## UPI Circle: delegation depth

UPI Circle, as reported, delegates one level: a primary user adds a secondary user and sets a per-transaction or monthly limit, in full or partial delegation. This project keeps the same shape at the root (the user's Intent Mandate names one agent and its caps) and then allows onward delegation between agents, because that is the arrangement a planner-and-specialist agent system actually needs.

| UPI Circle (reported) | Here |
|---|---|
| One level: primary → secondary | Many levels: intent → sub-mandate → sub-mandate, capped at `MAX_DELEGATION_LINKS = 8` |
| The primary sets the secondary's limit | Every link sets its delegate's limits, and R19 refuses any link that is not a subset of its parent — a delegation can only shrink |
| Spend counted against the delegated limit | Spend counted against *every* link at once (`chain_ids` on each capture), so sub-mandates cannot multiply the root's budget |
| Full vs partial delegation (whether the primary approves each payment) | The step-up token is the equivalent of "primary approves this one": user-signed, bound to one cart and amount |

The honest gap is the same as everywhere else here: UPI Circle is a rail with real user enrolment and NPCI-side enforcement, while this is a local policy gate over Razorpay test mode. Depth beyond one level is our design choice, not something UPI Circle offers.

## Google AP2 (Agent Payments Protocol)
Three signed mandates, Intent, Cart and Payment, as verifiable credentials forming a chain. Here: the same three objects plus an Agent Proposal (so the merchant's cart can be checked against what the agent asked for) and a Step-Up Token, signed as Ed25519 envelopes over canonical JSON rather than W3C VCs. The gate's chain rules (R06, R08, R11) are what make the chain a chain.

AP2's chain runs user → agent → merchant for one purchase. The Sub-Mandate extends it sideways, agent → agent: each link is signed by the previous link's agent, names it as `parent_id`, and may only narrow it (R18 for the shape, R19 for the subset). Two more objects continue the chain past the payment: a merchant-signed Shortfall Attestation and a gate-signed Refund Mandate, so money going back is as much a signed, checkable chain as money going out. AP2 does not specify these; they are this project's answer to "who is allowed to authorize the reverse leg".

## OpenAI/Stripe ACP (Agentic Commerce Protocol)
ACP-inspired field names (`item_id`, `title`, `description`, `url`, price, availability, `image_url`), not the ACP product-feed schema; the `.well-known/agent-commerce.json` file is our own discovery convention. Here: `merchant_data/feed.json` and `merchant_data/.well-known/agent-commerce.json`. No ACP checkout API or shared payment tokens; the merchant answers a proposal with a signed, price-locked Cart Mandate instead.

## MCP
Razorpay ships an official MCP server and its Agent Studio is built on an agent SDK. Here the agent's two tools (`browse_catalog`, `propose_cart`) are plain function-calling tools over an OpenAI-compatible chat-completions API; exposing the merchant as an MCP server is listed as future work. Deliberately, the agent is not given the Razorpay MCP server: that would put the LLM in the trust path, which is the one thing this design refuses to do.

## x402
HTTP 402 machine payments in stablecoins. Landscape only; off-rails for INR / Razorpay.
