"""The refund rules engine (M2 task 1): a pure function from facts to a Decision.

    decide_refund(order, today, claims) -> RefundDecision

No database, no model, no clock read, no side effect. The inputs are the order snapshot with its
items and shipment state, `today` (obtained from the injected Clock by the caller) and,
separately, the customer's structured claims. The customer's MESSAGE is not an input.
Everything that decides an outcome or an amount is in this file; the model only explains the
result (CLAUDE.md).

Two dates, two types, two jobs (SPEC 16.1):
  * the policy version is selected by the PURCHASE date (`select_policy_version(PurchaseDate)`);
  * the window is measured from the WINDOW ANCHOR (`WindowAnchorDate`): the delivery date, or the
    presumed-lost date when there was no delivery.
They never share a variable, and a test fails if they are ever driven by the same date.

Evaluation order (first match ends it):
  1  version selected by purchase date           none in effect -> needs_human
  2  subscription charge / canceled order        routed away / denied
  3  window anchor: delivered, lost, exception   undelivered restricted categories -> needs_human
  4  window                                      outside -> denied
  5  per-line eligibility (final_sale, accessed digital add-on, proration), from the amount PAID
  6  the paid-amount INVARIANT clamp             a system rule, never a policy clause
  7  aggregate, then the auto-approve ceiling    above it -> needs_human (human review, NOT denial)
Claims are evaluated last and can only ESCALATE (see "Claims").

Per-line amounts come from `paid_cents` of the line (already net of the discount allocation) and
proration is applied to THAT: discount first, then proration (SPEC 16.2). Shipping is never
refundable: refund lines are per item, and a header must equal the sum of its lines (the drift
views). Integer cents throughout; proration rounds DOWN, so it can never exceed what was paid.

Two paid-amount mechanisms, kept visibly separate:
  * POLICY: refund v2 clause 4 ("the amount actually paid, never list price") is applied and cited
    when the order carries a discount (reason PAID_AMOUNT_BASIS_POLICY);
  * INVARIANT: refund v1 is silent, so under v1 no clause is cited and the reason is
    PAID_AMOUNT_BASIS_INVARIANT. `_enforce_paid_invariant` then caps every line and the order at
    what was paid minus what was already refunded, under both versions, and cites nothing.

DECISIVE vs SUPPORTING (SPEC 10.5). Every applied clause is one or the other. Decisive: the clause
the outcome and the amount rest on. Supporting: applied, but it did not bind (it located a date or a
threshold that was not reached). Concretely:
  allowed       decisive: the window clause; for a lost shipment the lost-shipment clause (v1)
                and the presumed-lost clause; for the early v2 path the exception clause; and
                every clause that shaped an amount (final_sale, accessed digital add-on,
                proration, v2 promo).
                supporting: the ceiling (checked, not binding).
  needs_human   (above the ceiling) decisive: the ceiling only. (category timeline conflict)
                decisive: BOTH conflicting clauses. (claim) none: everything applied is supporting.
  denied        decisive: the clause that denied (window expired; not yet presumed lost; the
                exclusions that zeroed every line). The rest is supporting.
The explanation must cite every decisive clause.

Where policy v2 is silent (it states no lost-shipment refund and no proration; both are in v1), the
rule is CARRIED FORWARD, reason RULE_CARRIED_FORWARD_FROM_V1, and no v2 clause is cited for it,
because none exists. That is an interpretation, reported in SPEC 18, not a silent one.

Claims. A `Claim` is a structured assertion of a fact the rules read and the records contradict. It
is never evidence for a grant. The engine re-evaluates assuming the claim TRUE; if the decision
(outcome or any amount) would differ, the outcome depends on a fact the system cannot verify and the
result is needs_human with UNVERIFIABLE_CLAIM and the claim text recorded verbatim. A claim can only
escalate: it never turns a denial or a handoff into an allowance and never raises an amount (a
property test asserts that). A claim the records already agree with, or that would change nothing,
is recorded in the facts and changes nothing.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import date
from enum import StrEnum

from app.domain.reason_codes import ReasonCode as R
from app.domain.reason_codes import unverifiable_claim_evidence
from app.domain.rules.shipping import SHIPPING_POLICY_VERSION, presumed_lost_date
from app.domain.types import PurchaseDate, WindowAnchorDate
from app.retrieval.policy_loader import chunk_key

RULE_SET_VERSION = "refund-1"

# Categories whose shipping_restrictions timelines conflict with the general shipping policy's
# (hazardous 14/20 business days, perishable 3/5, oversize 21; SPEC 16.5). Age-restricted items have
# no timeline of their own, so there is nothing to conflict (the order-status rule is stricter and
# also holds them; reported in SPEC 18).
CONFLICTING_TIMELINE_CATEGORIES = frozenset({"hazardous", "perishable", "oversize"})
_RESTRICTION_CLAUSE = {"hazardous": 3, "perishable": 4, "oversize": 6}  # shipping_restrictions v1


# --- policy terms ------------------------------------------------------------------------------


@dataclass(frozen=True)
class RefundTerms:
    """The numbers and clause numbers of one refund policy version. A test asserts every one of them
    against the stored policy text, so the code cannot drift from the policy."""

    version: int
    effective_from: date
    effective_to: date | None  # inclusive; None = open-ended
    window_days: int
    ceiling_cents: int
    exception_refund: bool  # v2 clause 3
    promo_paid_clause: int | None  # v2 clause 4
    window: int
    ceiling: int
    digital: int
    final_sale: int
    lost: int | None  # None: this version states no lost-shipment rule (carried forward from v1)
    proration: int | None  # None: this version states no proration rule (carried forward from v1)
    exception: int | None


TERMS: tuple[RefundTerms, ...] = (
    RefundTerms(
        version=1,
        effective_from=date(2025, 6, 1),
        effective_to=date(2026, 2, 28),
        window_days=30,
        ceiling_cents=7500,
        exception_refund=False,
        promo_paid_clause=None,
        window=1,
        ceiling=3,
        digital=5,
        final_sale=6,
        lost=2,
        proration=4,
        exception=None,
    ),
    RefundTerms(
        version=2,
        effective_from=date(2026, 3, 1),
        effective_to=None,
        window_days=45,
        ceiling_cents=12000,
        exception_refund=True,
        promo_paid_clause=4,
        window=1,
        ceiling=2,
        digital=5,
        final_sale=5,
        lost=None,
        proration=None,
        exception=3,
    ),
)


def select_policy_version(purchase: PurchaseDate) -> RefundTerms | None:
    """The refund policy in effect on the PURCHASE date; None if none was (before 2025-06-01)."""
    for t in TERMS:
        if t.effective_from <= purchase and (t.effective_to is None or purchase <= t.effective_to):
            return t
    return None


# --- inputs ------------------------------------------------------------------------------------


class ClaimKind(StrEnum):
    NOT_RECEIVED = "not_received"  # asserts a shipment recorded as delivered was not received
    DIGITAL_NOT_ACCESSED = "digital_not_accessed"  # asserts an add-on recorded as accessed was not


@dataclass(frozen=True)
class Claim:
    kind: ClaimKind
    verbatim: str  # the customer's words, recorded and never interpreted


@dataclass(frozen=True)
class RefundItem:
    item_id: str
    sku: str
    category: str
    paid_cents: int  # amount actually paid for the line, after discount allocation
    refunded_cents: int = 0  # already refunded (the running total on the line)
    final_sale: bool = False
    digital_addon: bool = False
    digital_accessed_on: date | None = None
    # Subscription box proration (refund v1 clause 4): items in the box, items shipped so far.
    box_items_total: int | None = None
    box_items_shipped: int | None = None

    def __post_init__(self) -> None:
        if self.paid_cents < 0 or self.refunded_cents < 0 or self.refunded_cents > self.paid_cents:
            raise ValueError(f"item {self.item_id}: need 0 <= refunded <= paid")
        if (self.box_items_total is None) != (self.box_items_shipped is None):
            raise ValueError("box_items_total and box_items_shipped go together")
        if self.box_items_total is not None and not (
            self.box_items_total > 0 and 0 <= (self.box_items_shipped or 0) <= self.box_items_total
        ):
            raise ValueError(f"item {self.item_id}: need 0 <= shipped <= total, total > 0")


@dataclass(frozen=True)
class RefundShipment:
    status: str  # label_created | in_transit | delivered | exception | lost
    shipped_on: date | None
    delivered_on: date | None
    has_delivery_exception: bool = False


@dataclass(frozen=True)
class RefundOrder:
    order_number: str
    purchase_date: PurchaseDate  # selects the policy version; never the window anchor
    status: str  # placed | shipped | delivered | canceled
    items: tuple[RefundItem, ...]
    shipment: RefundShipment | None
    kind: str = "purchase"  # purchase | subscription_charge
    list_total_cents: int = 0
    discount_cents: int = 0
    shipping_cents: int = 0
    paid_cents: int = 0
    refunded_total_cents: int = 0
    has_replacement: bool = False  # a replacement shipment was already sent for this order

    def __post_init__(self) -> None:
        if (
            min(self.paid_cents, self.discount_cents, self.shipping_cents, self.list_total_cents)
            < 0
        ):
            raise ValueError("money amounts are non-negative integer cents")
        if not 0 <= self.refunded_total_cents <= self.paid_cents:
            raise ValueError("need 0 <= refunded_total <= paid")


# --- output ------------------------------------------------------------------------------------


class RefundOutcome(StrEnum):
    ALLOWED = "allowed"  # auto-approved: at or below the ceiling
    DENIED = "denied"
    NEEDS_HUMAN = "needs_human"  # human review / handoff; the reason code says why


@dataclass(frozen=True)
class LineDecision:
    item_id: str
    sku: str
    paid_cents: int
    refunded_cents: int
    amount_cents: int  # what this line would refund (proposed if the outcome is needs_human)
    reason_codes: tuple[R, ...] = ()


@dataclass(frozen=True)
class AppliedClause:
    key: str  # chunk key, e.g. refund-v2-clause-1; these ARE the citations (SPEC 10.4)
    decisive: bool


@dataclass(frozen=True)
class Handoff:
    reason_code: R
    evidence: dict[str, object]  # what a human needs; claims recorded verbatim


@dataclass(frozen=True)
class RefundDecision:
    outcome: RefundOutcome
    lines: tuple[LineDecision, ...]
    reason_codes: tuple[R, ...]
    applied: tuple[AppliedClause, ...]
    policy_version: int  # 0: none in effect
    facts: dict[str, str | int | bool | None] = field(default_factory=dict)
    handoff: Handoff | None = None
    rule_set_version: str = RULE_SET_VERSION

    @property
    def aggregate_cents(self) -> int:
        """Always the sum of the lines (an invariant, asserted by a property test)."""
        return sum(line.amount_cents for line in self.lines)

    @property
    def authorized_cents(self) -> int:
        """What may be refunded now: the aggregate if allowed, else zero (SPEC decisions check)."""
        return self.aggregate_cents if self.outcome is RefundOutcome.ALLOWED else 0

    @property
    def applied_clause_keys(self) -> tuple[str, ...]:
        return tuple(c.key for c in self.applied)

    @property
    def decisive_clause_keys(self) -> tuple[str, ...]:
        return tuple(c.key for c in self.applied if c.decisive)

    @property
    def supporting_clause_keys(self) -> tuple[str, ...]:
        return tuple(c.key for c in self.applied if not c.decisive)


# --- the engine --------------------------------------------------------------------------------


def _iso(d: date | None) -> str | None:
    return None if d is None else d.isoformat()


class _Clauses:
    """Applied clause keys in first-use order; a key applied twice keeps the stronger role."""

    def __init__(self) -> None:
        self._roles: dict[str, bool] = {}

    def add(self, key: str | None, decisive: bool) -> None:
        if key is not None:
            self._roles[key] = self._roles.get(key, False) or decisive

    def only_decisive(self, keys: set[str | None]) -> None:
        """Demote every applied clause not in `keys` to supporting (and promote those in it)."""
        for k in self._roles:
            self._roles[k] = k in keys

    def result(self) -> tuple[AppliedClause, ...]:
        return tuple(AppliedClause(k, d) for k, d in self._roles.items())


def _refund_key(terms: RefundTerms, number: int | None) -> str | None:
    return None if number is None else chunk_key("refund", terms.version, number)


def decide_refund(order: RefundOrder, today: date, claims: Sequence[Claim] = ()) -> RefundDecision:
    """The Decision for a refund request against `order` as of `today`. Total: it never raises for a
    valid input and never reads a clock, a database or a model."""
    base = _evaluate(order, today)
    if not claims:
        return base
    recorded = tuple(c.verbatim for c in claims)
    for claim in claims:
        alt_order = _assume_claim_true(order, claim)
        if alt_order is None:  # the records already agree with the claim, or it concerns nothing
            continue
        alt = _evaluate(alt_order, today)
        if _signature(alt) != _signature(base):
            evidence = unverifiable_claim_evidence(recorded)
            evidence.update(
                {
                    "records_say": base.outcome.value,
                    "records_authorized_cents": base.authorized_cents,
                    "if_claim_true": alt.outcome.value,
                    "if_claim_true_authorized_cents": alt.authorized_cents,
                    "claim_kind": claim.kind.value,
                }
            )
            return RefundDecision(
                RefundOutcome.NEEDS_HUMAN,
                tuple(replace(line, amount_cents=0) for line in base.lines),
                (R.UNVERIFIABLE_CLAIM,),
                tuple(AppliedClause(c.key, False) for c in base.applied),  # supporting only
                base.policy_version,
                {**base.facts, "claims_recorded": len(recorded)},
                Handoff(R.UNVERIFIABLE_CLAIM, evidence),
            )
    return replace(base, facts={**base.facts, "claims_recorded": len(recorded)})


def _signature(d: RefundDecision) -> tuple[str, tuple[int, ...]]:
    return d.outcome.value, tuple(line.amount_cents for line in d.lines)


def _assume_claim_true(order: RefundOrder, claim: Claim) -> RefundOrder | None:
    """The order as the claim says it is, or None when the claim changes nothing the records say."""
    if claim.kind is ClaimKind.NOT_RECEIVED:
        ship = order.shipment
        if ship is None or ship.delivered_on is None:
            return None
        return replace(order, shipment=replace(ship, delivered_on=None, status="in_transit"))
    if claim.kind is ClaimKind.DIGITAL_NOT_ACCESSED:
        if not any(i.digital_accessed_on is not None for i in order.items):
            return None
        return replace(
            order, items=tuple(replace(i, digital_accessed_on=None) for i in order.items)
        )
    return None  # pragma: no cover - exhaustive over ClaimKind


def _result(
    outcome: RefundOutcome,
    reasons: Sequence[R],
    clauses: _Clauses,
    facts: dict[str, str | int | bool | None],
    version: int,
    lines: tuple[LineDecision, ...] = (),
    handoff: Handoff | None = None,
) -> RefundDecision:
    return RefundDecision(
        outcome, lines, tuple(dict.fromkeys(reasons)), clauses.result(), version, facts, handoff
    )


def _zero_lines(order: RefundOrder) -> tuple[LineDecision, ...]:
    return tuple(
        LineDecision(i.item_id, i.sku, i.paid_cents, i.refunded_cents, 0) for i in order.items
    )


def _evaluate(order: RefundOrder, today: date) -> RefundDecision:
    purchase: PurchaseDate = order.purchase_date
    terms = select_policy_version(purchase)  # the ONLY use of the purchase date
    facts: dict[str, str | int | bool | None] = {
        "order_number": order.order_number,
        "as_of": today.isoformat(),
        "purchase_date": purchase.isoformat(),
        "paid_cents": order.paid_cents,
        "discount_cents": order.discount_cents,
    }
    clauses = _Clauses()
    zero = _zero_lines(order)

    def handoff(reason: R, version: int, extra: list[R] | None = None) -> RefundDecision:
        return _result(
            RefundOutcome.NEEDS_HUMAN,
            [reason, *(extra or [])],
            clauses,
            facts,
            version,
            zero,
            Handoff(reason, {"reason_code": reason.value, "facts": dict(facts)}),
        )

    if terms is None:
        return handoff(R.POLICY_VERSION_NOT_IMPLEMENTED, 0)
    facts["policy_version"] = terms.version
    facts["window_days"] = terms.window_days
    facts["ceiling_cents"] = terms.ceiling_cents
    v = terms.version
    if order.kind == "subscription_charge":
        return handoff(R.SUBSCRIPTION_CHARGE_USES_CANCELLATION_RULES, v)  # SPEC 16.3 (task 2)
    if order.status == "canceled":
        return _result(RefundOutcome.DENIED, [R.NOTHING_REFUNDABLE], clauses, facts, v, zero)

    ship = order.shipment
    delivered_on = None if ship is None else ship.delivered_on
    if ship is not None and (
        (delivered_on is not None and delivered_on > today)
        or (ship.shipped_on is not None and ship.shipped_on > today)
        or (
            delivered_on is not None
            and ship.shipped_on is not None
            and delivered_on < ship.shipped_on
        )
        or any(
            i.digital_accessed_on is not None and i.digital_accessed_on > today for i in order.items
        )
    ):
        return handoff(R.RECORDS_INCONSISTENT, v)

    categories = {i.category for i in order.items}
    if delivered_on is None:
        conflict = sorted(categories & CONFLICTING_TIMELINE_CATEGORIES)
        if conflict:  # SPEC 16.5: do not resolve silently
            facts["categories"] = ",".join(conflict)
            clauses.add(chunk_key("shipping", SHIPPING_POLICY_VERSION, 2), True)
            for cat in conflict:
                clauses.add(chunk_key("shipping_restrictions", 1, _RESTRICTION_CLAUSE[cat]), True)
            return handoff(R.CATEGORY_TIMELINE_NOT_IMPLEMENTED, v)

    if ship is None or (delivered_on is None and ship.shipped_on is None):
        return handoff(R.NO_WINDOW_ANCHOR, v)

    # ---- the window anchor: a DIFFERENT date from the purchase date --------------------------
    anchor: WindowAnchorDate
    path: str
    reasons: list[R] = []
    window_key = _refund_key(terms, terms.window)
    if delivered_on is not None:
        anchor = WindowAnchorDate(delivered_on)
        path = "delivered"
    else:
        if ship.shipped_on is None:  # unreachable: guarded above; fail closed rather than assume
            return handoff(R.NO_WINDOW_ANCHOR, v)
        lost_on = presumed_lost_date(ship.shipped_on)
        presumed_lost_key = chunk_key("shipping", SHIPPING_POLICY_VERSION, 2)
        anchor = WindowAnchorDate(lost_on)
        facts["presumed_lost_on"] = lost_on.isoformat()
        exception = ship.has_delivery_exception or ship.status == "exception"
        if ship.status == "lost" or today >= lost_on:
            path = "lost"
            clauses.add(presumed_lost_key, True)
            clauses.add(_refund_key(terms, terms.lost), True)
            reasons.append(R.LOST_SHIPMENT_FULL_REFUND)
            if terms.lost is None:
                reasons.append(R.RULE_CARRIED_FORWARD_FROM_V1)
        elif exception and terms.exception_refund:
            path = "exception"
            clauses.add(_refund_key(terms, terms.exception), True)
            clauses.add(presumed_lost_key, False)
            reasons += [R.CARRIER_DELIVERY_EXCEPTION, R.EARLY_REFUND_ON_DELIVERY_EXCEPTION]
        else:
            facts["eligible_from"] = lost_on.isoformat()
            clauses.add(presumed_lost_key, True)
            clauses.add(_refund_key(terms, terms.lost), True)
            if exception:  # v1 has no exception clause: it does not shorten the wait
                reasons.append(R.CARRIER_DELIVERY_EXCEPTION)
            return _result(
                RefundOutcome.DENIED, [R.NOT_YET_REFUNDABLE, *reasons, R.NOT_YET_PRESUMED_LOST],
                clauses, facts, v, zero,
            )  # fmt: skip
    facts["window_anchor"] = anchor.isoformat()
    facts["anchor_basis"] = "delivery" if path == "delivered" else "presumed_lost"
    days = (today - anchor).days  # negative while the window has not started (exception path)
    facts["days_since_anchor"] = days
    if days > terms.window_days:  # day `window_days` is the last valid day
        clauses.add(window_key, True)
        clauses.only_decisive({window_key})  # the lost-path clauses only located the anchor
        return _result(
            RefundOutcome.DENIED, [R.REFUND_WINDOW_EXPIRED, *reasons], clauses, facts, v, zero
        )
    facts["days_left"] = terms.window_days - days
    clauses.add(window_key, True)
    reasons.insert(0, R.REFUND_WITHIN_WINDOW)

    # ---- per-line eligibility, from the amount actually paid ---------------------------------
    lines: list[LineDecision] = []
    fired_final = fired_digital = fired_proration = False
    for item in order.items:
        line_reasons: list[R] = []
        remaining = item.paid_cents - item.refunded_cents
        if item.final_sale:
            amount = 0
            fired_final = True
            line_reasons.append(R.FINAL_SALE_EXCLUDED)
        elif item.digital_addon and item.digital_accessed_on is not None:
            amount = 0
            fired_digital = True
            line_reasons.append(R.DIGITAL_ADDON_ACCESSED)
        else:
            eligible = item.paid_cents  # discount is already inside paid_cents: discount FIRST
            if (
                path == "delivered"
                and item.box_items_total is not None
                and item.box_items_shipped is not None
            ):  # ... then proration, rounded down
                eligible = item.paid_cents * (item.box_items_total - item.box_items_shipped)
                eligible //= item.box_items_total
                fired_proration = True
                line_reasons.append(R.PRORATED_BY_ITEMS_SHIPPED)
            amount = max(0, min(eligible - item.refunded_cents, remaining))
            if remaining == 0:
                line_reasons.append(R.ALREADY_REFUNDED)
        lines.append(
            LineDecision(
                item.item_id,
                item.sku,
                item.paid_cents,
                item.refunded_cents,
                amount,
                tuple(line_reasons),
            )  # fmt: skip
        )
    if fired_final:
        clauses.add(_refund_key(terms, terms.final_sale), True)
    if fired_digital:
        clauses.add(_refund_key(terms, terms.digital), True)
    if fired_proration:
        clauses.add(_refund_key(terms, terms.proration), True)
        reasons.append(R.PRORATED_BY_ITEMS_SHIPPED)
        if terms.proration is None:
            reasons.append(R.RULE_CARRIED_FORWARD_FROM_V1)
    if order.discount_cents > 0:
        if terms.promo_paid_clause is not None:  # v2: the POLICY says so
            clauses.add(_refund_key(terms, terms.promo_paid_clause), True)
            reasons.append(R.PAID_AMOUNT_BASIS_POLICY)
        else:  # v1 is silent: the SYSTEM INVARIANT covers it, and no clause is cited for it
            reasons.append(R.PAID_AMOUNT_BASIS_INVARIANT)
    if order.has_replacement:
        reasons.append(R.REPLACEMENT_ALREADY_SENT)  # recorded for the reviewer; not a rule

    amounts, capped = _enforce_paid_invariant(order, [line.amount_cents for line in lines])
    lines = [replace(line, amount_cents=a) for line, a in zip(lines, amounts, strict=True)]
    if capped:
        reasons.append(R.PAID_AMOUNT_INVARIANT_CAP)
    out_lines = tuple(lines)
    aggregate = sum(amounts)
    facts["aggregate_cents"] = aggregate

    if aggregate == 0:
        clauses.add(_refund_key(terms, terms.ceiling), False)
        # Only the exclusions that zeroed lines are decisive; the window just located the anchor.
        clauses.only_decisive(
            {
                _refund_key(terms, terms.final_sale) if fired_final else None,
                _refund_key(terms, terms.digital) if fired_digital else None,
                _refund_key(terms, terms.proration) if fired_proration else None,
            }
        )
        no_money = [R.NOTHING_REFUNDABLE, *(r for r in reasons if r is not R.REFUND_WITHIN_WINDOW)]
        return _result(RefundOutcome.DENIED, no_money, clauses, facts, v, out_lines)

    ceiling_key = _refund_key(terms, terms.ceiling)
    if aggregate > terms.ceiling_cents:  # above the ceiling: human review, NOT denial
        clauses.add(ceiling_key, True)
        clauses.only_decisive({ceiling_key})
        return _result(
            RefundOutcome.NEEDS_HUMAN, [R.ABOVE_AUTO_APPROVE_CEILING, *reasons], clauses, facts, v,
            out_lines,
            Handoff(R.ABOVE_AUTO_APPROVE_CEILING, {
                "reason_code": R.ABOVE_AUTO_APPROVE_CEILING.value,
                "proposed_cents": aggregate, "ceiling_cents": terms.ceiling_cents,
                "facts": dict(facts),
            }),
        )  # fmt: skip
    clauses.add(ceiling_key, False)  # checked, not binding
    return _result(RefundOutcome.ALLOWED, reasons, clauses, facts, v, out_lines)


def _enforce_paid_invariant(order: RefundOrder, amounts: list[int]) -> tuple[list[int], bool]:
    """The SYSTEM invariant (CLAUDE.md, SPEC 16.2), independent of any policy text: no line refunds
    more than it was paid minus what it already refunded, and the order's total refunds never exceed
    what the order was paid. Returns the (possibly reduced) amounts and whether anything was cut."""
    capped = False
    out: list[int] = []
    for item, amount in zip(order.items, amounts, strict=True):
        limit = item.paid_cents - item.refunded_cents
        if amount > limit:
            capped = True
        out.append(max(0, min(amount, limit)))
    room = order.paid_cents - order.refunded_total_cents
    over = sum(out) - room
    if over > 0:
        capped = True
        for i in range(len(out) - 1, -1, -1):  # trim from the last line, deterministically
            cut = min(out[i], over)
            out[i] -= cut
            over -= cut
            if over == 0:
                break
    return out, capped


__all__ = [
    "CONFLICTING_TIMELINE_CATEGORIES",
    "RULE_SET_VERSION",
    "TERMS",
    "AppliedClause",
    "Claim",
    "ClaimKind",
    "Handoff",
    "LineDecision",
    "RefundDecision",
    "RefundItem",
    "RefundOrder",
    "RefundOutcome",
    "RefundShipment",
    "RefundTerms",
    "decide_refund",
    "select_policy_version",
]
