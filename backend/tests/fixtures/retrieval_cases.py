"""Ground truth for the first retrieval recall measurement.

For each of the eight required seed cases: the intents (which route to policy types), the clauses
a CORRECT DECISION needs under the policy version in effect on the case's purchase / start date,
and two customer phrasings written WITHOUT looking at clause wording (so the measurement is not
keyed to the policy's own vocabulary).

"Needed" means the clause states a rule the decision or its explanation has to cite. Two facts
this ground truth makes visible, both reported to the PM rather than papered over:
  * refund v2 clause 5 only says the digital add-on and final_sale rules are "unchanged"; the
    rule text itself lives in v1, which date filtering correctly hides from a v2 purchase.
  * cancellation v2 clause 1 says "As v1, but the fee is removed"; the 60-day proration rule
    lives only in v1 clause 3, hidden from a v2 subscription.
So for cases 3, 4 and 8 the retrievable v2 text does not by itself state every rule the rules
engine applies. That is a property of the corpus, not of retrieval.

This is a development set: it is the set k was chosen on, so recall measured here is optimistic.
The held-out measurement happens with the M3 evaluation set.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class RecallCase:
    case_key: str  # the seed case key (app/seed/definitions.py)
    intents: tuple[str, ...]
    needed: frozenset[str]  # chunk keys
    queries: tuple[str, ...]
    delivery_issue: bool = False  # the shipment is late/missing: shipping policy is in scope


CASES: tuple[RecallCase, ...] = (
    RecallCase(
        "refund_v1_above_ceiling_human_review",
        ("refund_request",),
        frozenset({"refund-v1-clause-1", "refund-v1-clause-3"}),
        (
            "I want a refund for order SIM-1001, the boxes were more than I needed.",
            "Please refund my whole order SIM-1001, I changed my mind.",
        ),
    ),
    RecallCase(
        "promo_discount_paid_differs_from_list",
        ("refund_request",),
        frozenset({"refund-v2-clause-4", "refund-v2-clause-1"}),
        (
            "I used a discount code on order SIM-1002 and want a refund. How much do I get back?",
            "Refund order SIM-1002 please, I paid less than list price because of a promotion.",
        ),
    ),
    RecallCase(
        "digital_addon_accessed_with_refundable_items",
        ("refund_request",),
        frozenset({"refund-v2-clause-5", "refund-v2-clause-1"}),
        (
            "I want a refund for order SIM-1003 including the recipe library add-on I opened.",
            "Refund SIM-1003 please, the digital recipe add-on was not what I expected.",
        ),
    ),
    RecallCase(
        "final_sale_item_in_order",
        ("refund_request",),
        frozenset({"refund-v2-clause-5", "refund-v2-clause-1"}),
        (
            "The clearance mug in order SIM-1004 arrived chipped and I want my money back.",
            "Can I return the final sale mug from order SIM-1004 for a refund?",
        ),
    ),
    RecallCase(
        "lost_shipment_with_replacement_sent",
        ("refund_request", "order_status"),
        frozenset({"shipping-v1-clause-2", "shipping-v1-clause-3", "refund-v2-clause-1"}),
        (
            "My package SIM-2001 never arrived and the replacement has not either. "
            "Refund the missing shipment.",
            "Where is order SIM-2001? It is long overdue and I want my money back for it.",
        ),
        delivery_issue=True,
    ),
    RecallCase(
        "annual_v1_inside_proration_window",
        ("cancel_subscription",),
        frozenset({"cancellation-v1-clause-3", "cancellation-v1-clause-1"}),
        (
            "I signed up for the annual plan a few weeks ago and want to cancel and get money back.",
            "Cancel my annual subscription. Do I get a prorated refund?",
        ),
    ),
    RecallCase(
        "monthly_v2_inside_cooling_off",
        ("cancel_subscription",),
        frozenset({"cancellation-v2-clause-2"}),
        (
            "I only subscribed last week and changed my mind. Cancel it and refund my first payment.",
            "Cancel my monthly plan, I was charged just a few days ago.",
        ),
    ),
    RecallCase(
        "annual_v2_day10_cooling_off_beats_proration",
        ("cancel_subscription",),
        frozenset({"cancellation-v2-clause-2", "cancellation-v2-clause-1"}),
        (
            "I started the annual plan ten days ago, I want to cancel and get my money back.",
            "Cancel my annual plan that I bought 10 days ago and refund it.",
        ),
    ),
)
