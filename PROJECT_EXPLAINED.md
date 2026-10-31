# ProofOfBalance, explained

This document explains what the project does, how every part works, and why it was built this way.
For commands, see [README.md](README.md). For hands-on exercises, see [docs/game-day.md](docs/game-day.md).

---

## 1. The problem

Payment companies change infrastructure all the time: a new base image, a Helm value, a Terraform tweak,
a dependency bump. The usual safety net asks *"is the service still up?"* Health checks, smoke tests
and error rates all pass while the system quietly does something worse: it **moves money differently**.

Examples that keep every dashboard green:

* A rounding mode changes from banker's rounding to round-half-up. Each payment moves by at most one paisa.
  Across millions of payments, that is real money, and the books no longer match the bank.
* A time-zone setting changes. Payments made at 23:59 in India now settle on a different value date.
  Nothing errors, but reconciliation with the bank breaks.
* A timeout gets shorter than the payout rail's latency. The worker gives up, retries, and the beneficiary
  is paid twice. Your database stays tidy (its primary key blocks the second row), yet the money has left.

ProofOfBalance turns the question into **"is every balance still exactly the same?"** and refuses to merge
or promote anything where the answer is no.

---

## 2. The big picture: two gates

| | Gate 1: before merge | Gate 2: in production |
|---|---|---|
| Where | GitHub Actions, on a throwaway kind cluster | AWS, k3s on one EC2 instance |
| Input | One recorded day of traffic (5,000 payments) | Live "trickle" traffic |
| Check | Ledger identical to the committed baseline, every payment balanced | Canary's failed-settlement **value** < 0.5%, zero duplicate payouts |
| Catches | Deterministic money changes (rounding, rates, time zones, fees, rejected payments) | Changes that only appear under real conditions (timeouts, load, rail latency) |
| On failure | Red required check + PR comment per corridor | Argo Rollouts aborts and scales the canary away |

---

## 3. The settlement service

### 3.1 What it does

A small FX settlement API (Python 3.12, FastAPI, PostgreSQL 17) that pays out US dollars as Indian rupees,
Mexican pesos or Brazilian reais. Corridors: `USD-INR`, `USD-MXN`, `USD-BRL`.

* `POST /payments` accepts a payment and writes it **plus five ledger postings in one transaction**.
* A **payout worker** (same image, different command) claims pending payments with
  `SELECT … FOR UPDATE SKIP LOCKED`, calls the rail, and marks them `paid` (adding two more postings) or `failed`.
* `GET /payments/{id}`, `GET /stats`, `GET /healthz`, `GET /readyz` (checks DB and schema), `GET /metrics`.

### 3.2 Double-entry bookkeeping

Every movement of money is recorded as postings that **sum to zero per currency**. Debits are negative,
credits positive. For a $100 USD→INR payment at 83.25 with a 20 bps fee:

| Leg | Account | Currency | Amount | Meaning |
|---|---|---|---|---|
| usd_in | client:42:USD | USD | −100.00 | client pays $100 |
| usd_in | pool:USD | USD | +99.80 | our USD pool receives the net |
| fee | revenue:fx_fee:USD | USD | +0.20 | fee revenue (0.2%) |
| fx_out | pool:INR | INR | −8,308.35 | INR leaves our pool (99.80 × 83.25) |
| fx_out | payout_clearing:INR | INR | +8,308.35 | owed to the beneficiary, not yet sent |
| payout | payout_clearing:INR | INR | −8,308.35 | *(after the rail pays)* clearing emptied |
| payout | beneficiary:7:INR | INR | +8,308.35 | *(after the rail pays)* beneficiary credited |

USD: −100.00 + 99.80 + 0.20 = 0. INR: −8,308.35 + 8,308.35 (−8,308.35 + 8,308.35) = 0. If any payment ever
fails this check, money was created or destroyed. The **reconciler** checks exactly this every 30 seconds.

A failed payout keeps its five postings: the rupees stay in `payout_clearing` for operations to return.

### 3.3 Determinism: the precondition for the whole idea

The diff only works if the same input always produces the same ledger **byte for byte**. Each rule removes one
source of noise:

| Rule | Where | Why |
|---|---|---|
| `Decimal` everywhere; passing a `float` raises `TypeError` | `service/app/money.py` | `0.1 + 0.2 != 0.3` in binary floating point |
| API accepts `usd_amount` only as a string like `"250.00"` | `service/app/main.py` | a JSON number is parsed as a float before we see it |
| Rates file values must be quoted strings | `service/app/rates.py` | YAML turns `83.25` into a float; the loader refuses it |
| Round with `quantize(Decimal("0.01"), rounding=ROUNDING_MODE)` | `money.round_money` | one explicit, configurable rounding rule |
| `id = uuid5(NAMESPACE, idempotency_key)`, never `uuid4()` | `service/app/ledger.py` | the same payment gets the same id in every run |
| Timestamps from the request's `occurred_at`, never `now()` | `ledger.create_payment` | wall-clock time differs per run |
| `value_date` = `occurred_at` in `SETTLEMENT_TZ` | `ledger.value_date` | explicit, configurable business date |
| Rail failure = `sha256(payment_id) % 1000 < FAIL_PERMILLE` | `rail/app.py` | the same payments fail every run |
| Export sorted in code-point order in Python | `tools/export_ledger.py` | database collations differ between machines |

**Verified:** replay + export on two fresh stacks gave byte-identical 38,764-row CSVs.

### 3.4 Idempotency and racing retries

Clients retry. The service guarantees that one idempotency key equals one payment:

1. `INSERT … ON CONFLICT DO NOTHING RETURNING id`. If the key exists, nothing is written.
2. Because the unique index serialises concurrent inserts, a racing duplicate waits for the first transaction
   to commit, then sees the existing row.
3. A repeat with the **same body** returns the original (`200`, header `Idempotent-Replayed: true`).
   A repeat with a **different body** is a client bug: `409 Conflict` plus the original payment.

The integration test fires 16 identical requests at once and asserts exactly one `201`, fifteen `200`s,
one row, and **one** transfer at the rail.

### 3.5 The payout worker and what "failed" means

The worker holds the row lock while it calls the rail, then commits the outcome in the same transaction:

| Rail answer | Worker does |
|---|---|
| `200` + `tx_hash` | status `paid`, two payout postings |
| `422` (rail refused) | status `failed`: retrying cannot help |
| timeout / connection error / `5xx` | unknown outcome: `attempts += 1`, retry with exponential backoff; after `MAX_ATTEMPTS`, `failed` |

The "unknown outcome" row is where real duplicate payouts come from: if the rail actually paid before the
timeout, the retry pays again. The project does not hide this. It **detects** it (see 3.6), which is how real
payment systems discover the problem.

Workers shut down gracefully on `SIGTERM` (finish the in-flight payout, then exit), so rollouts don't cut
transfers in half.

### 3.6 The mock stablecoin rail: the outside world

`rail/` is a deliberately simple, deliberately **non-idempotent** payout network:

* `POST /transfer` returns `tx_hash = sha256(payment_id)`.
* `DELAY_MS` adds latency; `FAIL_PERMILLE` fails a deterministic subset of payments.
* It remembers every `payment_id` it was asked to pay and counts repeats:
  `pob_rail_duplicate_transfers_total`.
* `GET /transfers` lists how many times each payment was paid.

Duplicates are counted **at the rail, not in our database**. The database cannot see a double payout because its
primary key blocks the second posting. The money has still left. Real duplicate payouts are found the same way:
by the outside world (bank statements, chain explorers), after the fact.

### 3.7 Validation and failure handling (edge cases)

| Input / situation | Result |
|---|---|
| Amount as JSON number, `"0.00"`, negative, 3 decimals, exponent, leading zero, spaces | `422` |
| Amount above `MAX_USD_AMOUNT` (default $10,000,000) | `422` |
| Unknown corridor, unknown fields, missing fields, ids that are strings / floats / ≤ 0 / > 2⁵³ | `422` |
| Timestamp without a time zone, or not a timestamp | `422` |
| Amount so small the fee eats it, or converts to 0.00 | `422` with the reason |
| Same key, different body | `409` with the original payment |
| Database down | `503` with `Retry-After: 2`; `/readyz` reports not ready; process stays up and reconnects |
| Schema not migrated yet | `/readyz` → `503`; worker waits instead of crashing |
| Invalid `ROUNDING_MODE`, `SETTLEMENT_TZ`, rates file, ints | process refuses to start, with the exact reason |
| Rail returns `200` without a `tx_hash`, or garbage | treated as unknown → retry |
| Metric labels from user input | unknown corridors are labelled `other` (bounded cardinality) |

---

## 4. Recorded traffic

`traffic/generator.py` writes `traffic/day1.jsonl`, one JSON object per line, seeded with `random.Random(42)`.
`python traffic/generator.py --check` proves the committed file still regenerates byte for byte (CI runs it).

What is in it, and what each part is there to catch:

| Content | Count | Exposes |
|---|---|---|
| Corridor mix | INR 2,453 / MXN 1,547 / BRL 1,000 | per-corridor regressions |
| `0.01`, `0.05`, `999,999.99` | 12 / 13 / 6 | precision and overflow |
| Fees exactly on a half cent (`2.50`, `7.50`, …) | 60 planted | rounding-mode changes (USD side) |
| Converted values exactly on a half cent | 40 per corridor planted, plus ~25% of INR naturally | rounding-mode changes (destination side) |
| Retries reusing an earlier key, 1–12 lines later | 150 (3%) | duplicate payouts under parallel replay |
| 18:00–19:00 UTC burst (past midnight in India) | 775 | `SETTLEMENT_TZ` changes |

The generator imports the service's own `money.py` to find the half-cent amounts, so there is one source of
truth for the maths.

---

## 5. Replay, export, diff

### `tools/replay.py`
Validates the whole file first (valid JSON, required fields, increasing `seq`, amounts as strings), waits for
`/readyz`, then sends every line with 8 threads pulling from one queue in `seq` order. The parallelism is
deliberate: it makes retried keys race. Transient failures (connection errors, `5xx`) are retried with backoff;
`4xx` are counted, not retried. It then polls `/stats` until nothing is pending (5-minute timeout).
Exit codes: `0` ok, `1` errors or unsettled payouts, `2` bad input / service never ready.

### `tools/export_ledger.py`
One SQL join of payments and postings, plus one **`rail` row per payment** whose amount is the number of
transfers the rail received. A payment the rail never paid shows `0`; a payment it paid twice shows `2`;
a transfer for a payment our ledger has never heard of becomes a `rail-orphan:<id>` row. Sorted in Python,
written atomically.

### `tools/diff_ledger.py`
Keys rows by `(idempotency_key, leg, account)` and classifies every difference:
**missing, extra, amount changed, duplicate payout, status changed, value date changed, tx hash changed,
corridor changed**. It also checks the invariant on the new run (postings sum to zero per payment per currency;
`rail` rows are counts and are skipped) and fails on an imbalance even if baseline and run are identical.

*Net change* per corridor is the change in what we owe beneficiaries (the `fx_out` credit to `payout_clearing`,
destination currency) and in fee revenue (USD). Those are the two numbers a rounding or rate change moves.

Exit codes: `0` identical and balanced, `1` any difference or imbalance, `2` unreadable input (the report still
says why).

### Results recorded on this machine

| Change | Verdict |
|---|---|
| none | PASS: 38,764 rows identical |
| `ROUNDING_MODE=ROUND_HALF_UP` | FAIL: INR 339, MXN 67, BRL 48 payments, *amount changed*, still balanced |
| `SETTLEMENT_TZ=Asia/Kolkata` | FAIL: 1,246 payments, *value date changed* |
| rail latency 300 ms vs worker timeout 100 ms | FAIL: 4,850 *duplicate payouts* (each paid 5× at the rail), all *status changed* to failed |

---

## 6. The CI gate (`.github/workflows/ledger-gate.yml`)

Two jobs, both meant to be **required status checks** on `main`:

* **unit-tests**: 205 unit tests, traffic reproducibility, `helm lint`, `bash -n` on every script.
* **ledger-gate**: builds both images, starts kind, installs the Helm chart with `values-ci.yaml`
  (`--wait --wait-for-jobs`), then runs `scripts/ci/gate.sh`: auto-restarting port-forwards → replay → export
  → diff. It posts (or edits) **one** PR comment, writes the job summary, uploads `run.csv`, `report.md`,
  `diff_rows.csv` and pod logs as the artifact `ledger-run`, and fails if the ledger changed.

Design details that matter:

* **No `paths:` filter on the trigger.** A required check skipped by a paths filter leaves the PR waiting
  forever. The job always runs; `dorny/paths-filter` skips the heavy steps for README-only changes.
* **Baseline changes need a label.** If `baseline/` changed and the PR is not labelled `update-baseline`, the job
  fails. Nobody can silently accept a money change by regenerating the baseline in the same PR.
* **The gate never fails silently.** If the cluster, port-forward or export breaks, `gate.sh` still writes a
  report that says the *gate* failed (exit 3), which is different from *the ledger changed* (exit 1).
* `scripts/kind-gate.sh` runs the identical flow on your laptop.

### `build.yml`
On merge to `main`: build both images, **Trivy** scan (fails on fixable CRITICAL vulnerabilities), push to
GHCR tagged with the commit SHA and `latest`.

### `terraform.yml`
PRs touching `infra/`: `fmt -check`, `validate`, and `plan` posted as a PR comment, using the read-only OIDC
plan role. Apply/destroy only via `workflow_dispatch` on `main`, through the protected `production` environment
(manual approval), using the separate apply role.

---

## 7. Kubernetes packaging (`helm/proofofbalance`)

One chart deploys to kind (CI) and k3s (AWS):

| Template | What |
|---|---|
| `postgres.yaml` | StatefulSet with official `postgres:17` (upgraded from 16 in demo PR #1), PVC on AWS (retained on uninstall), emptyDir in CI |
| `migrate-job.yaml` | a new Job per Helm revision running every idempotent migration in one transaction, behind an advisory lock |
| `service.yaml` | API Deployment + Service (`pob-service`), readiness on `/readyz` |
| `worker.yaml` | payout worker: plain Deployment, or an **Argo Rollout** with 5 replicas when `worker.rollout.enabled` |
| `analysistemplate.yaml` | the value-based canary analysis (below) |
| `rail.yaml` | exactly one replica, `Recreate` (its state is in memory) |
| `reconciler.yaml` | the 30-second invariant check |
| `trickle.yaml` | live traffic for canaries |
| `monitoring.yaml` | PodMonitor, PrometheusRule, Grafana dashboard ConfigMap |
| `configmap-rates.yaml` | `rates.yaml` rendered with every value quoted |

The chart symlinks `db/migrations`, `monitoring/alert-rules.yaml` and `monitoring/grafana-dashboard.json`
instead of copying them, so there is one source of truth. Helm follows the links and prints a notice.

Every app container runs as UID 10001 with a read-only root filesystem, no privilege escalation, all
capabilities dropped and the `RuntimeDefault` seccomp profile. Changing rates rolls pods automatically
(checksum annotation).

---

## 8. AWS with Terraform (`infra/terraform`)

| File | Resources | Notes |
|---|---|---|
| `backend.tf` | S3 backend, `use_lockfile = true` | S3-native locking, no DynamoDB; bucket passed at init |
| `providers.tf` | AWS provider `~> 6.0`, `default_tags` | `Project`, `Owner`, `Environment`, `ManagedBy` on everything |
| `network.tf` | dedicated VPC (`10.20.0.0/16`), public subnet, IGW, routes, security group | 22 and 6443 from your IP(s) as `/32` only; 80/443 open; AZ chosen where the instance type is offered. The VPC range is validated not to overlap k3s's pod (`10.42/16`) and service (`10.43/16`) networks: an overlap silently breaks in-cluster routing (pods cannot reach the API, CoreDNS never becomes ready) |
| `compute.tf` | Ubuntu 24.04 AMI lookup, key pair, **Elastic IP allocated first**, instance, `aws_ec2_instance_state` | IMDSv2, encrypted gp3, standard CPU credits, `ignore_changes = [ami]` |
| `user_data.sh.tftpl` | k3s install with `--tls-san <EIP>`, 2 GB swap, retries, done-marker | the EIP must be in the API cert or `kubectl` from your laptop is rejected. Deliberately *no* `--node-external-ip`: k3s would advertise the public IP as the in-cluster API endpoint and pods would hairpin through the internet gateway into the locked-down security group |
| `oidc.tf` | GitHub OIDC provider (or reuse an existing one), **plan role** + **apply role** | plan role: read-only + state; apply role: only `environment:production` |
| `budget.tf` | monthly budget, email at 80% actual / 100% forecast | |
| `outputs.tf` | IP, instance id, SSH and kubeconfig commands, role ARNs | |

**Why OIDC matters:** GitHub Actions gets short-lived AWS credentials for each run instead of long-lived
access keys stored as secrets. Nothing to leak, nothing to rotate.

**Instance type:** `t3.medium` by default. AWS *Free plan* accounts may only launch Free-Tier-eligible
types (EC2's DryRun does not even check this; the real launch fails), so `setup.sh` reads the account plan
through the Free Tier API and switches to `m7i-flex.large` (2 vCPU, 8 GB, eligible). Terraform only sets
CPU credits for burstable `t*` types.

**Cost discipline:** burstable types use *standard* CPU credits (a runaway process is throttled, not billed), a
`$10` budget with alerts, `teardown.sh --pause` (stop compute) and `teardown.sh` (destroy everything,
then verify by tag that nothing billable is left).

---

## 9. Observability

| Metric | Type | Labels | Emitted by |
|---|---|---|---|
| `pob_settlement_value_usd_total` | counter | corridor, status (`paid`/`failed`) | payout worker |
| `pob_payments_total` | counter | corridor, status | service |
| `pob_settlement_latency_seconds` | histogram | corridor | payout worker |
| `pob_idempotent_replays_total` | counter | corridor | service |
| `pob_rail_transfers_total` | counter | currency | rail |
| `pob_rail_duplicate_transfers_total` | counter | currency | rail |
| `pob_ledger_unbalanced_payments` | gauge | currency | reconciler |
| `pob_rail_retries_total` | counter | corridor, reason | payout worker (extra) |
| `pob_reconciler_last_success_timestamp_seconds` | gauge | none | reconciler (extra) |

Alerts (`monitoring/alert-rules.yaml`): **LedgerUnbalanced** (critical), **DuplicatePayout** (critical),
**FailedValueHigh** (> 0.5% of value over 5m), **SlowSettlement** (p95 > 2s for 5m), and **ReconcilerStale**
(without the reconciler, LedgerUnbalanced could never fire). Alertmanager routes to Discord or Slack when a
webhook is set in `.env`.

The Grafana dashboard shows a red/green stat for unbalanced payments and duplicate payouts, the failed-value
ratio, settled value per corridor, p95 latency, canary-vs-stable failure ratio by pod-template-hash, retries
and pod restarts.

Logs are one JSON object per line with `payment_id`, `corridor` and `status`.

---

## 10. Canary releases gated on settlement value

The payout worker is an Argo **Rollout** with 5 replicas. With no service mesh, Argo splits by replica count:
`setWeight: 20` = 1 canary pod of 5. All workers pull from the same queue, so the canary settles about 20% of
payouts. No Istio needed.

The PodMonitor copies the `rollouts-pod-template-hash` pod label onto every metric, so Prometheus can tell
canary from stable. The AnalysisTemplate then measures, every minute for 5 minutes:

* **failed-value-ratio**: for the canary's hash only, per corridor, `USD value of failed or timed-out attempts /
  USD value of all attempts`; the *worst* corridor must stay `< 0.005`. A timed-out attempt (status `retry`) counts
  against the pod that made it, even if a stable pod pays that payment later. Without that, a canary that times
  out every MXN payout looks healthy: its retries get finished by stable pods (demo PR #6 is exactly this).
* **no-duplicate-payouts**: `increase(pob_rail_duplicate_transfers_total[2m])` must be `0`.

Steps: 20% → analysis → 60% → pause 2 min → 100%. Any failed analysis aborts and scales the canary away.

**Why value, not request count:** one failed $50,000 payout matters more than a hundred failed $5 ones.
Measuring failed value is how a payments company actually thinks about risk. **Why per corridor:** MXN is about a
third of the traffic; a corridor that is completely broken must not hide inside a healthy-looking total.

**What CI cannot see:** on AWS the MXN rail answers in ~300 ms (`rail.delayMsByCurrency` in `values-aws.yaml`);
in CI the mock rail answers instantly. A worker timeout of 100 ms therefore passes the ledger gate and fails in
production, which is why the canary layer exists.

**Why the trickle exists:** with no payments the ratio is 0/0, and the analysis fails instead of passing.
The trickle Deployment replays `day1.jsonl` slowly in a loop with fresh idempotency keys, mounted from a
gzipped ConfigMap.

---

## 11. The two scripts you run

### `scripts/setup.sh`: one command, safe to repeat

Stages (each logged with its duration; a failure stops with an explanation):

1. **Load configuration**: parses `.env` without executing it, validates key formats, budget, email and image
   source; generates and remembers Postgres/Grafana passwords in `.secrets/generated.env`.
2. **Prerequisites**: installs pinned, SHA-256-verified `kubectl`, `helm`, `terraform` into `.tools/bin` if
   missing; creates `.venv`.
3. **Check AWS credentials**: STS identity; explains `InvalidClientTokenId`, `SignatureDoesNotMatch`,
   `ExpiredToken`; warns about root keys.
4. **Resolve inputs**: detects *every* public IP this machine egresses from (some networks rotate between
   several; each becomes its own `/32` rule), picks an instance type the account may launch (Free plan →
   `m7i-flex.large`), generates an SSH key.
5. **Terraform state bucket**: creates it if missing (versioned, encrypted, public access blocked) and
   detects a bucket name owned by someone else.
6. **Terraform apply**: detects an existing GitHub OIDC provider; retries once (IAM eventual consistency);
   translates state-lock, permission, capacity and Elastic-IP-limit errors into next steps. A paused
   instance is started again.
7. **Wait for k3s**: SSH and cloud-init progress, prints the bootstrap log on failure, fetches the kubeconfig
   (rewritten to the public IP), and forgets an old host key if the instance was replaced. If port 6443 is
   blocked by your network (common on campus and office networks), it switches kubectl to an **SSH tunnel**
   (`127.0.0.1:16443 → node:6443`, managed by `scripts/tunnel.sh`) instead of exposing the API more widely.
8. **Monitoring, Argo Rollouts**: `helm upgrade --install`, and a release stuck in `pending-*` or `failed`
   from an interrupted run is rolled back or reinstalled first.
9. **Application images**: builds locally and copies into k3s over SSH (skipped if the node already has that
   exact content-addressed tag), or uses GHCR.
10. **Deploy**: namespace, Postgres secret, traffic ConfigMap, chart, waits up to 15 minutes; on failure
    prints pod status, events and logs. Refuses to start if `POSTGRES_PASSWORD` no longer matches the
    existing volume.
11. **Smoke test**: `helm test`, then a real payment over a port-forward that must be paid with 7 balanced postings.
12. **GitHub settings** (only with `GITHUB_TOKEN`): Actions variables, label, environment, branch protection.

`scripts/setup.sh --local` does the same for docker compose without AWS.

### `scripts/teardown.sh`: stop paying

* Default: stop local stacks, `terraform destroy`, then list anything still tagged `Project=ProofOfBalance`
  and fail loudly if an instance, Elastic IP or volume survived.
* `--pause`: stop only the EC2 instance (`aws_ec2_instance_state`); `setup.sh` resumes it.
* `--force-cleanup`: if Terraform cannot destroy (lost state, errors), terminate tagged instances, release
  Elastic IPs and delete volumes directly.
* `--purge-state`: also delete the state bucket, after you type its name.

Both scripts write `logs/<name>-status.txt`: SUCCESS or FAILED, the failed stage, what it means, the
completed stages, a summary, and the last 40 log lines.

---

## 12. Tests

| Suite | Count | What |
|---|---|---|
| `tests/unit/test_money.py` | money maths | spec example, every corridor × awkward amount × rounding mode balances, half-cent behaviour, floats rejected, edge amounts |
| `tests/unit/test_config_rates_ledger.py` | config | YAML floats rejected, malformed rates, env validation, `uuid5` stability, value dates across time zones |
| `tests/unit/test_api.py` | API | 17 invalid-field cases, 409, 503, 404, readiness, metrics, bad config refuses to start |
| `tests/unit/test_rail.py` | rail | deterministic failure rate, duplicate counting, invalid requests not recorded |
| `tests/unit/test_worker.py` | worker | paid/failed/retry/backoff/max-attempts decisions with a fake DB and mocked rail |
| `tests/unit/test_diff_ledger.py` | diff | every classification, net change, invariant, malformed CSVs exit 2 |
| `tests/unit/test_tools.py` | tools | replay validation, retries, parallelism; export rail rows and ordering; generator reproducibility |
| `tests/unit/test_aws_helper.py` | AWS helper | credential error explanations, state-bucket edge cases (owned elsewhere, name taken), all egress IPs, Free-plan instance choice (stubbed AWS) |
| `tests/unit/test_shell_lib.py` | shell library | `.env` parsing never executes code, precedence, secrets are private (600), retry/probe, failure status report |
| `tests/unit/test_repo_consistency.py` | repository | chart rates = service rates, chart symlinks, alert rules, dashboard panels, baseline balanced and sorted, no secrets in `.env.example` |
| `tests/integration/test_stack.py` | 9 | against docker compose: 5→7 postings, 16 racing duplicates → 1 payment and 1 rail transfer, 409, corridors, reconciler catches tampering |

Unit: 205 passed. Integration: 9 passed. Gate scenarios: see section 5.

---

## 13. Interview talking points

* *"Most infra tests ask whether the service is up. Mine asks whether every balance is still identical.
  A rounding change that moves one paisa per payment passes every health check and fails my gate."*
* *"Determinism is the hard part: Decimal everywhere, uuid5, no now(), fixed rates, collation-independent
  sorting. Two fresh runs produce byte-identical ledgers."*
* *"Duplicate payouts are counted at the rail, not in my DB, because my primary key hides them. That's how
  they're found in real life: by the outside world, after the money has left."*
* *"The canary is judged on failed settlement value, not error count. One failed $50k payout outweighs a
  hundred failed $5 ones."*
* *"CI gets AWS access through OIDC: short-lived credentials per run, a read-only plan role for PRs and an
  apply role only an approved environment can assume. No keys to leak or rotate."*
* *"Cost: one t3.medium with k3s instead of EKS, standard CPU credits, a budget alert, and a teardown script
  that verifies by tag that nothing billable is left."*
