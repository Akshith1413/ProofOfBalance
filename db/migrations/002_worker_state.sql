-- Operational columns for the payout worker's retry loop.
-- None of these are exported to the ledger CSV, so they never affect the gate.

ALTER TABLE payments ADD COLUMN IF NOT EXISTS attempts    INT NOT NULL DEFAULT 0;
ALTER TABLE payments ADD COLUMN IF NOT EXISTS retry_after TIMESTAMPTZ;
ALTER TABLE payments ADD COLUMN IF NOT EXISTS last_error  TEXT;
