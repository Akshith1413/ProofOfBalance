-- ProofOfBalance ledger schema.
-- Idempotent: safe to run on every deploy (the migration Job and docker compose
-- both re-run every file in order inside one transaction).

CREATE TABLE IF NOT EXISTS payments (
  id              UUID PRIMARY KEY,                       -- uuid5(namespace, idempotency_key)
  idempotency_key TEXT UNIQUE NOT NULL,
  corridor        TEXT NOT NULL,                          -- 'USD-INR'
  usd_amount      NUMERIC(20,2) NOT NULL CHECK (usd_amount > 0),
  rate            NUMERIC(20,8) NOT NULL CHECK (rate > 0),
  status          TEXT NOT NULL DEFAULT 'pending'
                  CHECK (status IN ('pending', 'paid', 'failed')),
  tx_hash         TEXT,
  occurred_at     TIMESTAMPTZ NOT NULL,
  value_date      DATE NOT NULL,
  -- Not in the original sketch: the payout worker needs the beneficiary to
  -- write the payout leg, and a retried key must be compared to the original.
  client_id       BIGINT NOT NULL CHECK (client_id > 0),
  beneficiary_id  BIGINT NOT NULL CHECK (beneficiary_id > 0)
);

CREATE TABLE IF NOT EXISTS postings (
  payment_id UUID NOT NULL REFERENCES payments(id),
  leg        TEXT NOT NULL CHECK (leg IN ('usd_in', 'fee', 'fx_out', 'payout')),
  account    TEXT NOT NULL,                               -- 'client:42:USD', 'pool:INR'
  currency   CHAR(3) NOT NULL,
  amount     NUMERIC(20,2) NOT NULL,                      -- signed: debit < 0, credit > 0
  PRIMARY KEY (payment_id, leg, account)
);

-- The payout worker polls this with FOR UPDATE SKIP LOCKED.
CREATE INDEX IF NOT EXISTS payments_pending_idx
  ON payments (occurred_at, id) WHERE status = 'pending';
