"""HELD-OUT retrieval queries (task 7C). Four phrasings per seeded case, written BEFORE any
retrieval was run on them, and never used to choose k or any other setting.

The selection set (tests/fixtures/retrieval_cases.py, two phrasings per case) is the set k is
chosen on; this set is the honest figure. The needed clauses are the same per case: the ground
truth belongs to the case, not to the phrasing. Phrasings are deliberately more varied than the
selection set (amounts, dates, coupon talk, "broke after a week", "grace period") so some of them
collide with the distractor documents (warranty windows, gift card refunds, the archived FAQ).

Do not edit these queries after looking at results.
"""

HELDOUT_QUERIES: dict[str, tuple[str, ...]] = {
    "refund_v1_above_ceiling_human_review": (
        "Hi, order SIM-1001 came to almost 98 dollars and honestly I do not need it. Can I send it "
        "back and how much would you refund?",
        "How many days after delivery can I still return order SIM-1001, and will someone have to "
        "approve a refund this large?",
        "I would like my money back for SIM-1001. It is a big order, is there some limit above "
        "which you check refunds manually?",
        "Return request for SIM-1001: delivered mid March, I changed my mind about the family box.",
    ),
    "promo_discount_paid_differs_from_list": (
        "I bought SIM-1002 with a 25 percent promo code. If I return it do I get the sticker price "
        "back or what I actually paid?",
        "Does the refund on a discounted order go back at the discounted amount? Order SIM-1002, "
        "delivered late March.",
        "Can you refund SIM-1002? I used a coupon and I want to know the window and the amount.",
        "Return order SIM-1002 please, how long do I have since it arrived and is the discount "
        "deducted?",
    ),
    "digital_addon_accessed_with_refundable_items": (
        "I unlocked the recipe library that came with SIM-1003 but the boxes were wrong. Can I "
        "return everything including the add-on?",
        "Is the online recipe add-on from order SIM-1003 refundable once I have opened it?",
        "Refund SIM-1003 - I only used the digital recipes once. What counts?",
        "My order SIM-1003 had a digital extra I already looked at. Do I lose that part of the "
        "refund?",
    ),
    "final_sale_item_in_order": (
        "The mug I bought on clearance in order SIM-1004 is cracked. Can final sale items be sent "
        "back?",
        "I want a refund for the discounted mug in SIM-1004 even though it said no returns.",
        "The clearance item in SIM-1004 broke after a week. Am I covered or is final sale final?",
        "Return the final-sale mug from SIM-1004 and refund the box too please.",
    ),
    "lost_shipment_with_replacement_sent": (
        "Order SIM-2001 still has not turned up and the replacement is late too. When do you "
        "consider a parcel lost and can I get refunded?",
        "How long do I wait before a missing package counts as lost? It is SIM-2001, shipped "
        "March 11th.",
        "Tracking for SIM-2001 stopped at a hub weeks ago. You already sent a replacement; what is "
        "the refund window for the original?",
        "My parcel SIM-2001 is lost, I got a replacement shipped but I want the money for the "
        "first one.",
    ),
    "annual_v1_inside_proration_window": (
        "I took the yearly subscription in February and want out. Do I get part of the money back?",
        "Cancel my annual plan started 20 Feb. Is there an admin fee on a prorated refund?",
        "How many days into an annual plan can I still cancel for a partial refund?",
        "Stop my yearly box, and tell me if the remaining months are refunded.",
    ),
    "monthly_v2_inside_cooling_off": (
        "I signed up for the monthly box a few days ago by mistake. Can I cancel and get today's "
        "charge back?",
        "Is there a grace period after I am first charged where I can cancel for a full refund?",
        "Cancel my monthly subscription started 8 April, I was only charged last week.",
        "Do you refund the first payment if I cancel straight away?",
    ),
    "annual_v2_day10_cooling_off_beats_proration": (
        "I joined the annual plan on April 5th. If I cancel now do I get everything back or only "
        "part?",
        "Ten days into my yearly subscription and I want to cancel. Which refund rule applies, the "
        "full one or the prorated one?",
        "Cancel annual plan bought 5 April, fee or no fee on the refund?",
        "Within two weeks of the first charge on my annual plan, do I get the whole payment back?",
    ),
}
