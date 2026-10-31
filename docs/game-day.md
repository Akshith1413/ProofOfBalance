# Game days

Planned exercises showing that each safety net catches what it is meant to catch.
Run them, take screenshots for `docs/screenshots/`, and write down what you saw.

All `kubectl` commands assume `export KUBECONFIG=$PWD/.secrets/kubeconfig` (written by `scripts/setup.sh`).

---

## Gate 1: before merge (local, no AWS needed)

Each run starts a fresh stack, replays `traffic/day1.jsonl` and diffs against
`baseline/ledger_baseline.csv`. These results were recorded on this machine.

| Scenario (what a PR would change) | Command | Verdict |
|---|---|---|
| Nothing that touches money | `scripts/local-gate.sh` | PASS, exit 0 |
| Rounding mode `ROUND_HALF_EVEN` → `ROUND_HALF_UP` | `scripts/local-gate.sh ROUNDING_MODE=ROUND_HALF_UP` | FAIL: 3 corridors, 454 payments, *amount changed* |
| Settlement time zone `UTC` → `Asia/Kolkata` | `scripts/local-gate.sh SETTLEMENT_TZ=Asia/Kolkata` | FAIL: 1,246 payments, *value date changed* (everything after 18:30 UTC, including the 18:00–19:00 burst) |
| Payout timeout shorter than rail latency | `scripts/local-gate.sh RAIL_DELAY_MS=300 RAIL_TIMEOUT_MS=100` | FAIL: *duplicate payout* on every payment (the rail was paid 5×), then *status changed* to failed |
| Rail failure rate 5‰ → 50‰ | `scripts/local-gate.sh RAIL_FAIL_PERMILLE=50` | FAIL: 225 payments (INR 111, MXN 72, BRL 42), *status changed* to failed, payout legs *missing* |

The same scenarios on Kubernetes, exactly as CI runs them:

```bash
scripts/kind-gate.sh --set settlement.roundingMode=ROUND_HALF_UP
scripts/kind-gate.sh --set settlement.timezone=Asia/Kolkata
scripts/kind-gate.sh --set rail.failPermille=50
```

### Demo pull requests for the GitHub UI

1. Branch `demo/readme-only`: edit `README.md` only. **ledger-gate** finishes in seconds as *skipped* (green).
2. Branch `demo/harmless`: raise `service.resources.limits.memory` in `values-ci.yaml`. Full replay, **passes**.
3. Branch `demo/rounding`: set `settlement.roundingMode: ROUND_HALF_UP` in `helm/proofofbalance/values.yaml`.
   **Fails**, and the PR comment shows the per-corridor table.
4. Branch `demo/timezone`: set `settlement.timezone: Asia/Kolkata`. **Fails** with *value date changed*.
5. Branch `demo/sneaky-baseline`: run 3, then also commit the new `baseline/ledger_baseline.csv` without the
   `update-baseline` label. **Fails**: baseline changes need the label.

---

## Gate 2: in production (AWS; run `scripts/setup.sh` first)

Keep a terminal watching the canary:

```bash
kubectl argo rollouts -n pob get rollout pob-worker --watch
```

And Grafana open (dashboard **ProofOfBalance — settlement**):

```bash
kubectl -n monitoring port-forward svc/kube-prometheus-stack-grafana 3000:80
```

### A. A healthy release is promoted

```bash
helm upgrade pob helm/proofofbalance -n pob -f helm/proofofbalance/values-aws.yaml --reuse-values \
  --set worker.threads=3
```

Expect: 1 canary pod out of 5 → analysis runs 5 × 1 min → `failed-value-ratio` ≈ 0 and
`no-duplicate-payouts` = 0 → 60% → 2 min pause → 100%.

### B. A release that times out too early is rolled back (duplicate payouts)

```bash
helm upgrade pob helm/proofofbalance -n pob -f helm/proofofbalance/values-aws.yaml --reuse-values \
  --set rail.delayMs=300 --set worker.railTimeoutMs=100
```

The rail is now slower than the canary worker's timeout. The canary times out, retries, and pays again.
The rail counts the repeats: `pob_rail_duplicate_transfers_total` rises, **DuplicatePayout** fires
(critical), `no-duplicate-payouts` fails and Argo **aborts and scales the canary back to 0**.
Afterwards, restore the rail: `--set rail.delayMs=20 --set worker.railTimeoutMs=2000`.

### C. Failed value above 0.5%

```bash
helm upgrade pob helm/proofofbalance -n pob -f helm/proofofbalance/values-aws.yaml --reuse-values \
  --set rail.failPermille=100 --set worker.threads=2
```

About 10% of payouts fail at the rail. **FailedValueHigh** fires after 2 minutes and the canary
analysis fails on `failed-value-ratio`. Restore with `--set rail.failPermille=0`.

### D. Money created out of thin air (reconciler)

```bash
kubectl -n pob exec pob-postgres-0 -- psql -U pob -c \
  "UPDATE postings SET amount = amount + 0.01 WHERE ctid = (SELECT ctid FROM postings WHERE account = 'pool:USD' LIMIT 1);"
```

Within 30 s `pob_ledger_unbalanced_payments{currency="USD"}` = 1, the red stat on the dashboard turns red,
and **LedgerUnbalanced** pages after 1 minute. Undo with the same statement using `- 0.01`.

### E. No traffic, no promotion

```bash
kubectl -n pob scale deploy/pob-trickle --replicas=0
```

Start any rollout: with no canary payouts the ratio is 0/0 and the analysis fails instead of
passing. This is why the trickle Deployment exists. Scale it back to 1 afterwards.

---

## Notes template

| Date | Exercise | Expected | Observed | Time to detect | Follow-up |
|---|---|---|---|---|---|
| | | | | | |
