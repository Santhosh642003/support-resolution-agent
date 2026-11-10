"""Distinct date types so the two policy anchors cannot be conflated.

- PurchaseDate selects WHICH policy version applies (purchase terms form part of the
  purchase contract).
- WindowAnchorDate is where the selected version's window STARTS: the delivery date, or the
  presumed-lost date when there was no delivery.

Rule functions take both, by type. Under mypy strict, passing one where the other is
expected is a type error at the call site, not a logic bug found in production.
"""

from datetime import date
from typing import NewType

PurchaseDate = NewType("PurchaseDate", date)
WindowAnchorDate = NewType("WindowAnchorDate", date)
