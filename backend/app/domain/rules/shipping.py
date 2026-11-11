"""Shipping-policy dates (SPEC 13, shipping policy v1; a single version).

The constants carry the policy version they implement, and a test asserts they equal the numbers
in the stored policy text, so a change to the policy cannot silently disagree with the code.
These functions only derive DATES from the ship date. Whether a refund is allowed is decided by
the (later) refund rules, not here.
"""

from datetime import date

from app.domain.rules.business_days import add_business_days

SHIPPING_POLICY_VERSION = 1
DELIVERY_WINDOW_BUSINESS_DAYS = 7  # "standard domestic delivery window: 7 business days"
PRESUMED_LOST_BUSINESS_DAYS = 10  # "presumed lost if undelivered 10 business days after ship date"


def expected_delivery_date(shipped_on: date) -> date:
    return add_business_days(shipped_on, DELIVERY_WINDOW_BUSINESS_DAYS)


def presumed_lost_date(shipped_on: date) -> date:
    return add_business_days(shipped_on, PRESUMED_LOST_BUSINESS_DAYS)
