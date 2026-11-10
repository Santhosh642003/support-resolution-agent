"""SQL for single-use confirmation tokens.

Expiry uses clock_timestamp(), not now() (which is frozen at transaction start).
Consumption is ONE conditional UPDATE, never read-then-write: zero rows returned means the
token was already used, expired, belongs to another customer, or names another decision.
"""

CONSUME_CONFIRMATION_TOKEN_SQL = """
UPDATE confirmation_tokens
SET used_at = now()
WHERE token_hash = :token_hash
  AND customer_id = :customer_id
  AND decision_id = :decision_id
  AND used_at IS NULL
  AND expires_at > clock_timestamp()
RETURNING decision_id, customer_id
"""  # noqa: S105
