"""Deterministic policy gate. Pure: no I/O, no clock, no LLM, no network. First failing rule decides."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

from mandatemesh.crypto import Envelope, verify
from mandatemesh.mandates import (
    AgentProposal,
    CartMandate,
    IntentMandate,
    MalformedMandate,
    PaymentMandate,
    ShortfallAttestation,
    StepUpToken,
    SubMandate,
)
from mandatemesh.registry import ACTIVE, AgentRegistry

ALLOW = "ALLOW"
DENY = "DENY"
STEP_UP = "STEP_UP"
MAX_DELEGATION_LINKS = 8


@dataclass
class Check:
    rule_id: str
    passed: bool
    detail: str


@dataclass
class Decision:
    verdict: str
    rule_id: str
    reason: str
    checks: list[Check] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class GateInput:
    intent: Envelope
    proposal: Envelope
    cart: Envelope
    user_pub_b64: str
    merchant_pubs: dict[str, str]
    spent_paise: int
    now: int
    stepup: Envelope | None = None
    chain: list[Envelope] = field(default_factory=list)  # sub-mandates, root-first, leaf last; empty when undelegated
    spent_by: dict[str, int] = field(default_factory=dict)  # spend per sub_id; spent_paise stays the root intent's


@dataclass
class RefundInput:
    """Everything the gate needs to authorize money going the other way. Same discipline: pure inputs, no I/O."""

    attestation: Envelope
    cart: Envelope
    payment: Envelope
    merchant_pubs: dict[str, str]
    gate_pub_b64: str
    captured_paise: int
    refunded_paise: int
    seen_shortfalls: list[str]
    now: int


def rupees(paise: int) -> str:
    sign, mag = ("-" if paise < 0 else ""), abs(paise)
    return f"INR {sign}{mag // 100:,}.{mag % 100:02d}"


@dataclass
class _Bound:
    """The spending bounds one link of the mandate chain grants, whether it is the root intent or a sub-mandate."""

    id: str
    agent_id: str
    currency: str
    max_total_paise: int
    max_per_txn_paise: int
    merchant_allowlist: list[str]
    categories: list[str]
    expires_at: int


def _bound_of(m: IntentMandate | SubMandate) -> _Bound:
    id_ = m.sub_id if isinstance(m, SubMandate) else m.intent_id
    return _Bound(id_, m.agent_id, m.currency, m.max_total_paise, m.max_per_txn_paise,
                  list(m.merchant_allowlist), list(m.categories), m.expires_at)


class PolicyGate:
    def __init__(self, registry: AgentRegistry) -> None:
        self.registry = registry

    def evaluate(self, gi: GateInput) -> Decision:
        checks: list[Check] = []
        try:
            return self._evaluate(gi, checks)
        except Exception as exc:  # the gate never raises: an internal error is a DENY, never an ALLOW
            detail = f"internal gate error: {type(exc).__name__}"
            checks.append(Check("R99_GATE_ERROR", False, detail))
            return Decision(DENY, "R99_GATE_ERROR", detail, checks)

    def _evaluate(self, gi: GateInput, checks: list[Check]) -> Decision:
        def ok(rule: str, detail: str) -> None:
            checks.append(Check(rule, True, detail))

        def fail(rule: str, detail: str, verdict: str = DENY) -> Decision:
            checks.append(Check(rule, False, detail))
            return Decision(verdict, rule, detail, checks)

        try:
            proposal = AgentProposal.from_payload(gi.proposal.payload)
            intent = IntentMandate.from_payload(gi.intent.payload)
            cart = CartMandate.from_payload(gi.cart.payload)
            subs = [SubMandate.from_payload(e.payload) for e in gi.chain]
        except MalformedMandate as exc:
            return fail("R00_WELL_FORMED", f"malformed mandate: {str(exc)[:200]}")
        ok("R00_WELL_FORMED", f"intent, proposal, cart and {len(subs)} sub-mandate payloads are well-formed" if subs
           else "intent, proposal and cart payloads are well-formed")

        rec = self.registry.get(proposal.agent_id)
        if rec is None:
            return fail("R01_AGENT_REGISTERED", f"agent {proposal.agent_id!r} is not in the trusted-agent registry")
        ok("R01_AGENT_REGISTERED", f"agent {proposal.agent_id!r} is registered")

        if rec.status != ACTIVE:
            return fail("R02_AGENT_ACTIVE", f"AGENT_REVOKED: agent {proposal.agent_id!r} status is {rec.status!r}")
        ok("R02_AGENT_ACTIVE", "agent status is active")

        if not verify(gi.proposal, rec.pubkey_b64):
            return fail("R03_PROPOSAL_SIG", "proposal signature does not verify against the registry key")
        ok("R03_PROPOSAL_SIG", "proposal signature verified against registry key")

        if not verify(gi.intent, gi.user_pub_b64):
            return fail("R04_INTENT_SIG", "intent mandate signature does not verify against the user key")
        ok("R04_INTENT_SIG", "intent mandate signature verified")

        if gi.now >= intent.expires_at:
            return fail("R05_INTENT_NOT_EXPIRED", f"intent expired at {intent.expires_at}; now is {gi.now}")
        ok("R05_INTENT_NOT_EXPIRED", f"intent valid until {intent.expires_at}")

        # R18, pass one: walk the delegation chain root-first and check only its shape -- each link signed by the previous
        # link's agent, parented to it, and no id repeated -- so a later R19 denial still leaves a recorded R18 verdict.
        links: list[_Bound] = [_bound_of(intent)]
        if len(subs) > MAX_DELEGATION_LINKS:
            return fail("R18_DELEGATION_CHAIN", f"delegation chain of {len(subs)} links exceeds the maximum of {MAX_DELEGATION_LINKS}")
        seen_ids = {links[0].id}
        for i, (env, sub) in enumerate(zip(gi.chain, subs)):
            parent = links[-1]
            rec = self.registry.get(sub.delegator_id)
            if rec is None or rec.status != ACTIVE:
                return fail("R18_DELEGATION_CHAIN", f"link {i} ({sub.agent_id!r}): delegator {sub.delegator_id!r} is not an active registered agent")
            if not verify(env, rec.pubkey_b64):
                return fail("R18_DELEGATION_CHAIN", f"link {i} ({sub.agent_id!r}): sub-mandate signature does not verify against {sub.delegator_id!r}")
            if sub.parent_id != parent.id:
                return fail("R18_DELEGATION_CHAIN", f"link {i} ({sub.agent_id!r}): parent_id {sub.parent_id!r} is not the previous link {parent.id!r}")
            if sub.delegator_id != parent.agent_id:
                return fail("R18_DELEGATION_CHAIN", f"link {i} ({sub.agent_id!r}): delegator {sub.delegator_id!r} is not the previous link's agent {parent.agent_id!r}")
            if sub.sub_id in seen_ids:
                return fail("R18_DELEGATION_CHAIN", f"link {i} ({sub.agent_id!r}): sub_id {sub.sub_id!r} repeats an earlier link")
            seen_ids.add(sub.sub_id)
            links.append(_bound_of(sub))
        if subs:
            summary = " -> ".join(b.agent_id for b in links)
            if len(summary) > 200:
                summary = f"{links[0].agent_id!r} -> ... -> {links[-1].agent_id!r}"
            ok("R18_DELEGATION_CHAIN", f"{len(subs)}-link delegation chain verified: {summary}")
        else:
            ok("R18_DELEGATION_CHAIN", "no delegation: proposal is under the root mandate")

        # R19, pass two: a link may only narrow what its parent granted, so the leaf's bounds are a subset of every ancestor's.
        for i, sub in enumerate(subs):
            parent = links[i]
            if sub.currency != parent.currency:
                return fail("R19_DELEGATION_SUBSET", f"link {i} ({sub.agent_id!r}): currency {sub.currency} != parent {parent.currency}")
            if sub.max_total_paise > parent.max_total_paise or sub.max_per_txn_paise > parent.max_per_txn_paise:
                return fail("R19_DELEGATION_SUBSET", f"link {i} ({sub.agent_id!r}): caps {rupees(sub.max_total_paise)}/{rupees(sub.max_per_txn_paise)} exceed parent {rupees(parent.max_total_paise)}/{rupees(parent.max_per_txn_paise)}")
            if not set(sub.merchant_allowlist) <= set(parent.merchant_allowlist):
                return fail("R19_DELEGATION_SUBSET", f"link {i} ({sub.agent_id!r}): merchants {sorted(set(sub.merchant_allowlist) - set(parent.merchant_allowlist))} are not in the parent's allow-list")
            if not set(sub.categories) <= set(parent.categories):
                return fail("R19_DELEGATION_SUBSET", f"link {i} ({sub.agent_id!r}): categories {sorted(set(sub.categories) - set(parent.categories))} are not in the parent's categories")
            if sub.expires_at > parent.expires_at:
                return fail("R19_DELEGATION_SUBSET", f"link {i} ({sub.agent_id!r}): expires_at {sub.expires_at} is later than the parent's {parent.expires_at}")
            if gi.now >= sub.expires_at:
                return fail("R19_DELEGATION_SUBSET", f"link {i} ({sub.agent_id!r}): sub-mandate expired at {sub.expires_at}")
        ok("R19_DELEGATION_SUBSET", "every link narrows or equals its parent" if subs else "no delegation: nothing to narrow")
        leaf = links[-1]

        if proposal.agent_id != leaf.agent_id:
            return fail("R06_INTENT_AGENT_MATCH", f"mandate chain delegates to {leaf.agent_id!r} but proposal is from {proposal.agent_id!r}")
        ok("R06_INTENT_AGENT_MATCH", "proposal comes from the delegated agent")

        merchant_pub = gi.merchant_pubs.get(cart.merchant_id)
        if merchant_pub is None or not verify(gi.cart, merchant_pub):
            return fail("R07_CART_SIG", f"cart signature does not verify for merchant {cart.merchant_id!r}")
        ok("R07_CART_SIG", f"cart mandate signature verified for merchant {cart.merchant_id!r}")

        if proposal.intent_id != intent.intent_id:
            return fail("R08_CART_CHAIN", f"proposal references intent {proposal.intent_id!r}, not {intent.intent_id!r}")
        if cart.intent_id != intent.intent_id:
            return fail("R08_CART_CHAIN", f"cart references intent {cart.intent_id!r}, not {intent.intent_id!r}")
        if cart.proposal_id != proposal.proposal_id:
            return fail("R08_CART_CHAIN", f"cart references proposal {cart.proposal_id!r}, not {proposal.proposal_id!r}")
        if proposal.merchant_id != cart.merchant_id:
            return fail("R08_CART_CHAIN", f"proposal was addressed to {proposal.merchant_id!r} but the cart is from {cart.merchant_id!r}")
        ok("R08_CART_CHAIN", "proposal and cart both reference this intent; cart references this proposal and merchant")

        if gi.now >= cart.expires_at:
            return fail("R09_CART_NOT_EXPIRED", f"cart quote expired at {cart.expires_at}; now is {gi.now}")
        ok("R09_CART_NOT_EXPIRED", f"cart quote valid until {cart.expires_at}")

        computed = sum(i.qty * i.unit_price_paise for i in cart.items)
        if not cart.items:
            return fail("R10_CART_TOTAL_INTEGRITY", "cart has no lines")
        bad_lines = [i.sku for i in cart.items if i.qty < 1 or i.unit_price_paise < 0]
        if bad_lines:
            return fail("R10_CART_TOTAL_INTEGRITY", f"non-positive quantity or negative price on {bad_lines}")
        if computed != cart.total_paise or cart.total_paise <= 0:
            return fail("R10_CART_TOTAL_INTEGRITY", f"cart total {cart.total_paise} != sum of lines {computed}, or not positive")
        ok("R10_CART_TOTAL_INTEGRITY", f"cart total {rupees(cart.total_paise)} equals the sum of {len(cart.items)} positive lines")

        cart_lines = sorted((i.sku, i.qty) for i in cart.items)
        proposal_lines = sorted((i.sku, i.qty) for i in proposal.items)
        if cart_lines != proposal_lines:
            return fail("R11_CART_MATCHES_PROPOSAL", f"cart lines {cart_lines} differ from proposed lines {proposal_lines}")
        ok("R11_CART_MATCHES_PROPOSAL", "cart items match the agent's proposal")

        if cart.merchant_id not in leaf.merchant_allowlist:
            return fail("R12_MERCHANT_ALLOWED", f"merchant {cart.merchant_id!r} is not in the allow-list {leaf.merchant_allowlist}")
        ok("R12_MERCHANT_ALLOWED", f"merchant {cart.merchant_id!r} is allow-listed")

        bad = sorted({i.category for i in cart.items} - set(leaf.categories))
        if bad:
            return fail("R13_CATEGORY_ALLOWED", f"categories {bad} are not permitted by the mandate {leaf.categories}")
        ok("R13_CATEGORY_ALLOWED", "all item categories are permitted")

        if cart.currency != leaf.currency:
            return fail("R17_CURRENCY_MATCH", f"cart currency {cart.currency} != mandate currency {leaf.currency}")
        ok("R17_CURRENCY_MATCH", f"currency {cart.currency}")

        stepup_ok: bool | None = None
        stepup_detail = "no step-up token supplied"
        if gi.stepup is not None:
            stepup_ok, stepup_detail = self._check_stepup(gi, intent, cart)

        # R14/R15 hold against every link, root first: the root's spend is spent_paise, each sub-mandate's is spent_by[sub_id].
        # One check per rule is recorded, naming the root-most link that breached it, or the tightest link when none did.
        def spent_under(b: _Bound) -> int:
            return gi.spent_paise if b is links[0] else gi.spent_by.get(b.id, 0)

        total = cart.total_paise
        breach: dict[str, str] = {}
        for b in links:
            spent = spent_under(b)
            if "R14_PER_TXN_CAP" not in breach and total > b.max_per_txn_paise:
                breach["R14_PER_TXN_CAP"] = f"first breach root-first: link {b.agent_id!r}: cart {rupees(total)} exceeds the per-transaction cap {rupees(b.max_per_txn_paise)}"
            if "R15_TOTAL_CAP" not in breach and spent + total > b.max_total_paise:
                breach["R15_TOTAL_CAP"] = f"first breach root-first: link {b.agent_id!r}: spent {rupees(spent)} + cart {rupees(total)} exceeds the total cap {rupees(b.max_total_paise)}"
        tight_txn = min(links, key=lambda b: b.max_per_txn_paise)
        tight_total = min(links, key=lambda b: b.max_total_paise - spent_under(b))
        within = (
            ("R14_PER_TXN_CAP", f"cart {rupees(total)} is within every per-transaction cap (tightest: {tight_txn.agent_id!r} {rupees(tight_txn.max_per_txn_paise)})"),
            ("R15_TOTAL_CAP", f"projected spend {rupees(spent_under(tight_total) + total)} is within every total cap (tightest: {tight_total.agent_id!r} {rupees(tight_total.max_total_paise)})"),
        )
        for rule, within_detail in within:
            if rule not in breach:
                ok(rule, within_detail)
            elif gi.stepup is None:
                return fail(rule, breach[rule] + "; human step-up required", STEP_UP)
            elif not stepup_ok:
                return fail("R16_STEPUP_TOKEN_VALID", stepup_detail)
            else:
                ok(rule, breach[rule] + "; covered by step-up approval")

        if gi.stepup is not None:
            if not stepup_ok:
                return fail("R16_STEPUP_TOKEN_VALID", stepup_detail)
            ok("R16_STEPUP_TOKEN_VALID", stepup_detail)

        return Decision(ALLOW, "ALLOW", f"all {len(checks)} checks passed; authorizing {rupees(cart.total_paise)} to '{cart.merchant_id}'", checks)

    def _check_stepup(self, gi: GateInput, intent: IntentMandate, cart: CartMandate) -> tuple[bool, str]:
        assert gi.stepup is not None
        if not verify(gi.stepup, gi.user_pub_b64):
            return False, "step-up token signature does not verify against the user key"
        try:
            tok = StepUpToken.from_payload(gi.stepup.payload)
        except MalformedMandate as exc:
            return False, f"step-up token malformed: {exc}"
        if tok.intent_id != intent.intent_id or tok.cart_id != cart.cart_id:
            return False, "step-up token is bound to a different intent or cart"
        if gi.now >= tok.expires_at:
            return False, f"step-up token expired at {tok.expires_at}"
        if tok.approved_total_paise < cart.total_paise:
            return False, f"step-up approved {rupees(tok.approved_total_paise)} but cart is {rupees(cart.total_paise)}"
        return True, f"step-up {tok.stepup_id} approves {rupees(tok.approved_total_paise)} for cart {cart.cart_id}"

    # --- Refunds. Money going back is still a money action, so it goes through the same kind of gate. ---

    def evaluate_refund(self, ri: RefundInput) -> Decision:
        checks: list[Check] = []
        try:
            return self._evaluate_refund(ri, checks)
        except Exception as exc:  # same guard as evaluate(): an internal error is a DENY, never an ALLOW
            detail = f"internal gate error: {type(exc).__name__}"
            checks.append(Check("RF99_GATE_ERROR", False, detail))
            return Decision(DENY, "RF99_GATE_ERROR", detail, checks)

    def _evaluate_refund(self, ri: RefundInput, checks: list[Check]) -> Decision:
        def ok(rule: str, detail: str) -> None:
            checks.append(Check(rule, True, detail))

        def fail(rule: str, detail: str, verdict: str = DENY) -> Decision:
            checks.append(Check(rule, False, detail))
            return Decision(verdict, rule, detail, checks)

        try:
            att = ShortfallAttestation.from_payload(ri.attestation.payload)
            cart = CartMandate.from_payload(ri.cart.payload)
            payment = PaymentMandate.from_payload(ri.payment.payload)
        except MalformedMandate as exc:
            return fail("RF00_WELL_FORMED", f"malformed mandate: {str(exc)[:200]}")
        ok("RF00_WELL_FORMED", "attestation, cart and payment mandate payloads are well-formed")

        merchant_pub = ri.merchant_pubs.get(cart.merchant_id)
        if merchant_pub is None or not verify(ri.cart, merchant_pub):
            return fail("RF01_CART_SIG", f"cart signature does not verify for merchant {cart.merchant_id!r}")
        ok("RF01_CART_SIG", f"cart mandate signature verified for merchant {cart.merchant_id!r}")

        if not verify(ri.payment, ri.gate_pub_b64):
            return fail("RF02_PAYMENT_SIG", "payment mandate signature does not verify against the gate key")
        if payment.cart_id != cart.cart_id:
            return fail("RF02_PAYMENT_SIG", f"payment mandate is for cart {payment.cart_id!r}, not {cart.cart_id!r}")
        ok("RF02_PAYMENT_SIG", f"payment mandate {payment.payment_id!r} verified and bound to cart {cart.cart_id!r}")

        if not verify(ri.attestation, merchant_pub):
            return fail("RF03_ATTESTATION_SIG", f"shortfall attestation does not verify for merchant {cart.merchant_id!r}")
        if att.cart_id != cart.cart_id:
            return fail("RF03_ATTESTATION_SIG", f"attestation is for cart {att.cart_id!r}, not {cart.cart_id!r}")
        if att.payment_id != payment.payment_id:
            return fail("RF03_ATTESTATION_SIG", f"attestation is for payment {att.payment_id!r}, not {payment.payment_id!r}")
        ok("RF03_ATTESTATION_SIG", f"attestation {att.shortfall_id!r} verified and bound to this cart and payment")

        if ri.now >= att.expires_at:
            return fail("RF04_ATTESTATION_NOT_EXPIRED", f"attestation expired at {att.expires_at}; now is {ri.now}")
        ok("RF04_ATTESTATION_NOT_EXPIRED", f"attestation valid until {att.expires_at}")

        if ri.captured_paise <= 0:
            return fail("RF05_PAYMENT_CAPTURED", f"nothing was captured against payment {payment.payment_id!r}; there is nothing to refund")
        ok("RF05_PAYMENT_CAPTURED", f"{rupees(ri.captured_paise)} captured against payment {payment.payment_id!r}")

        # RF06: the refund is priced from the SIGNED cart, never from the number the merchant put in the attestation.
        if not att.lines:
            return fail("RF06_SHORTFALL_INTEGRITY", "attestation lists no short lines")
        cart_lines = {line.sku: line for line in cart.items}
        computed = 0
        for short in att.lines:
            line = cart_lines.get(short.sku)
            if line is None:
                return fail("RF06_SHORTFALL_INTEGRITY", f"short line {short.sku!r} is not on the signed cart")
            if not 1 <= short.qty_short <= line.qty:
                return fail("RF06_SHORTFALL_INTEGRITY", f"short quantity {short.qty_short} for {short.sku!r} is not between 1 and the cart's {line.qty}")
            computed += short.qty_short * line.unit_price_paise
        if att.refund_paise != computed:
            return fail("RF06_SHORTFALL_INTEGRITY", f"attested refund {rupees(att.refund_paise)} != {rupees(computed)} priced from the signed cart lines")
        if att.refund_paise <= 0:
            return fail("RF06_SHORTFALL_INTEGRITY", f"refund amount {rupees(att.refund_paise)} is not positive")
        ok("RF06_SHORTFALL_INTEGRITY", f"{len(att.lines)} short line(s) price out to {rupees(att.refund_paise)} on the signed cart")

        refundable = ri.captured_paise - ri.refunded_paise
        if att.refund_paise > refundable:
            return fail("RF07_REFUND_WITHIN_CAPTURE", f"refund {rupees(att.refund_paise)} exceeds the {rupees(refundable)} still refundable on payment {payment.payment_id!r}")
        ok("RF07_REFUND_WITHIN_CAPTURE", f"refund {rupees(att.refund_paise)} is within the {rupees(refundable)} still refundable")

        if att.shortfall_id in ri.seen_shortfalls:
            return fail("RF08_NO_DUPLICATE", f"shortfall {att.shortfall_id!r} has already been refunded")
        ok("RF08_NO_DUPLICATE", f"shortfall {att.shortfall_id!r} has not been refunded before")

        return Decision(ALLOW, "ALLOW", f"all {len(checks)} checks passed; authorizing a refund of {rupees(att.refund_paise)} against {payment.payment_id}", checks)
