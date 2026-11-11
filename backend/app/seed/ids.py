"""Deterministic row ids: uuid5 of (kind, key). The same key is the same id in every database,
so tests and evaluation cases name seed rows by key and need no lookup."""

from uuid import UUID, uuid5

# Fixed namespace. Changing it changes every seeded id, so it never changes.
SEED_NAMESPACE = UUID("5f0c1d3a-0b7e-4c52-9a55-3d1e6b2f7a10")


def _id(kind: str, key: str) -> UUID:
    return uuid5(SEED_NAMESPACE, f"{kind}:{key}")


def customer_id(key: str) -> UUID:
    return _id("customer", key)


def product_id(sku: str) -> UUID:
    return _id("product", sku)


def order_id(key: str) -> UUID:
    return _id("order", key)


def subscription_id(key: str) -> UUID:
    return _id("subscription", key)


def item_id(order_key: str, index: int) -> UUID:
    return _id("item", f"{order_key}:{index}")


def shipment_id(order_key: str) -> UUID:
    return _id("shipment", order_key)
