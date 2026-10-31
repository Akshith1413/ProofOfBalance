# ProofOfBalance

**A CI gate that blocks any infrastructure change that would move money in the ledger, even by one paisa.**

Most teams test an infra change by asking *"is the service still up?"*. ProofOfBalance asks
*"is every balance still exactly the same?"*. Every pull request replays one recorded day of
payment traffic on a throwaway Kubernetes cluster and diffs the resulting double-entry ledger
against a committed baseline. In production, Argo Rollouts promotes a release only while the
**value** of failed settlements stays under 0.5% and the payout rail sees **zero** duplicate payouts.

![architecture](docs/architecture.svg)

For a full walk-through of how and why everything works, read **[PROJECT_EXPLAINED.md](PROJECT_EXPLAINED.md)**.
What was actually run and verified (tests, gate scenarios, a real canary promotion and a real rollback on AWS)
is in **[docs/results.md](docs/results.md)**.

---

## Quick start

### 1. Local (no AWS, ~5 minutes)

```bash
make venv          # Python deps into .venv
make test          # unit tests, no Docker needed
make up            # docker compose: Postgres, API, 2 payout workers, rail, reconciler + smoke test
make gate          # the ledger gate on your laptop: replay 5,000 payments, diff vs baseline -> PASS
make gate ARGS="ROUNDING_MODE=ROUND_HALF_UP"   # a money-moving change -> FAIL with a per-corridor report
make down
```

### 2. AWS (one command up, one command down)

1. Put credentials in `.env` (copy `.env.example`; `.env` is git-ignored).
   Optional but recommended: `BUDGET_ALERT_EMAIL`, and `DISCORD_WEBHOOK_URL` or `SLACK_WEBHOOK_URL` for alerts.
2. Create everything:

   ```bash
   scripts/setup.sh
   ```

   It installs missing CLIs (pinned, checksum-verified) into `.tools/bin`, creates the S3 state bucket,
   runs Terraform (VPC, EC2 + k3s, Elastic IP, GitHub OIDC roles, $10 budget), installs
   kube-prometheus-stack and Argo Rollouts, builds and ships the images, deploys the chart, and runs a
   smoke payment. **Re-running it is always safe**: it repairs whatever is missing and resumes a paused instance.
3. Stop paying:

   ```bash
   scripts/teardown.sh            # destroy everything (keeps only the tiny state bucket), verify nothing bills
   scripts/teardown.sh --pause    # or just stop the instance; setup.sh resumes it
   ```

Every run writes `logs/<script>-<time>.log` and `logs/<script>-status.txt`. When something fails, the status
file says **which stage failed, what it means, and what to do**, followed by the last 40 log lines.

| | Approximate cost (ap-south-1, on-demand) |
|---|---|
| Running (t3.medium or m7i-flex.large + 30 GB gp3 + Elastic IP) | ~$1.25–2.00 / day |
| Paused (`--pause`: disk + Elastic IP) | ~$0.20 / day |
| Destroyed (state bucket only) | < $0.01 / month |

**AWS Free-plan accounts** (created after July 2025) can only launch Free-Tier-eligible instance types, and
t3.medium is not one. `setup.sh` reads the account plan and switches to **m7i-flex.large** (2 vCPU, 8 GB,
eligible) automatically; usage is then drawn from the plan's credits.

---

## How the gate works

1. **Deterministic service.** `Decimal` everywhere (a float raises), `uuid5` ids from the idempotency key,
   timestamps from the request, rates from a fixed ConfigMap, rounding from `ROUNDING_MODE`, value date from
   `SETTLEMENT_TZ`. The same input always produces the same ledger, byte for byte.
2. **Recorded traffic.** `traffic/day1.jsonl`: 5,000 payments seeded with `random.Random(42)`, with awkward
   amounts (0.01, 999,999.99, exact half cents), 3% retried idempotency keys, and a burst just past midnight IST.
3. **Replay → export → diff.** `tools/replay.py` (8 parallel workers, so retries race),
   `tools/export_ledger.py` (sorted CSV, plus one row per payment with the number of transfers the
   *rail* received), `tools/diff_ledger.py` (classifies every difference, checks every payment still sums to
   zero per currency, writes the PR comment).
4. **Required check.** `.github/workflows/ledger-gate.yml` runs on every PR; `dorny/paths-filter` skips the
   heavy replay for docs-only changes so the required check never hangs.

Example PR comment (actual output for `ROUNDING_MODE=ROUND_HALF_UP`):

```
### Ledger gate: FAILED — 3 corridors changed

| Corridor | Payments affected | Net change | Kind |
|---|---|---|---|
| USD-BRL | 48 | +0.03 BRL, +0.07 USD | amount changed |
| USD-INR | 339 | -10.94 INR, +0.17 USD | amount changed |
| USD-MXN | 67 | -5.90 MXN, +0.34 USD | amount changed |

Invariant check: passed (every payment still balances)
```

---

## Repository layout

```
service/            settlement API, payout worker, reconciler, trickle (FastAPI, one image) + Dockerfile
rail/               mock stablecoin rail (deterministic failures, counts duplicate payouts) + Dockerfile
db/migrations/      SQL schema (idempotent) + migrate.sh
traffic/            generator.py, day1.jsonl (the recorded traffic, committed)
tools/              replay.py, export_ledger.py, diff_ledger.py
baseline/           ledger_baseline.csv (committed; change only in a PR labelled update-baseline)
helm/proofofbalance chart: service, worker (Deployment or Argo Rollout), rail, postgres, migration Job,
                    reconciler, trickle, PodMonitor, PrometheusRule, AnalysisTemplate, Grafana dashboard
infra/terraform/    VPC, security group, EC2 + k3s, Elastic IP, OIDC roles, budget, S3 backend
monitoring/         Grafana dashboard JSON, alert rules, kube-prometheus-stack values
.github/workflows/  ledger-gate.yml, build.yml, terraform.yml
scripts/            setup.sh, teardown.sh, local-gate.sh, kind-gate.sh, ci/gate.sh, helpers
tests/              unit (no Docker) and integration (against docker compose)
docs/               architecture.svg, game-day.md, results.md
```

---

## Commands

| Goal | Command |
|---|---|
| Unit tests | `make test` |
| Integration tests (stack running) | `make up && make test-integration` |
| Ledger gate on docker compose | `scripts/local-gate.sh [KEY=VALUE ...]` |
| Exact CI gate on a local kind cluster | `scripts/kind-gate.sh [--set key=value ...]` |
| Re-record the baseline | `scripts/local-gate.sh --update-baseline` → PR labelled `update-baseline` |
| Verify the traffic file is reproducible | `python traffic/generator.py --check` |
| AWS up / pause / down | `scripts/setup.sh` / `scripts/teardown.sh --pause` / `scripts/teardown.sh` |
| kubectl through SSH (networks that block 6443) | `scripts/tunnel.sh start` / `stop` / `status` |
| Watch a canary | `kubectl argo rollouts -n pob get rollout pob-worker --watch` |
| Grafana | `kubectl -n monitoring port-forward svc/kube-prometheus-stack-grafana 3000:80` |

Overridable knobs for `local-gate.sh`: `ROUNDING_MODE`, `SETTLEMENT_TZ`, `RAIL_DELAY_MS`, `RAIL_FAIL_PERMILLE`,
`RAIL_TIMEOUT_MS`, and the host ports `POB_SERVICE_PORT`, `POB_RAIL_PORT`, `POB_PG_PORT`.

---

## GitHub setup (once)

1. Push this repository to `github.com/Akshith1413/ProofOfBalance`.
2. Settings → Branches → protect `main` → require the status checks **`unit-tests`** and **`ledger-gate`**.
   Without that, the gate is only advice.
3. Create the label **`update-baseline`**, and an environment **`production`** with yourself as required reviewer.
4. After `scripts/setup.sh`, add repository *variables* `AWS_PLAN_ROLE_ARN`, `AWS_APPLY_ROLE_ARN`,
   `TF_STATE_BUCKET`, `TF_VAR_MY_IP`, `TF_VAR_SSH_PUBLIC_KEY` (values are printed in `logs/setup-status.txt`).

Steps 2–4 happen automatically if you put a fine-grained `GITHUB_TOKEN` (Administration + Variables:
read/write on this repo) in `.env` before running `scripts/setup.sh`.

`build.yml` pushes `ghcr.io/akshith1413/proofofbalance-{service,rail}:<sha>` on every merge; set
`IMAGE_SOURCE=ghcr` (and `IMAGE_TAG=<sha>`) in `.env` to deploy those instead of locally built images.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `setup FAILED at stage "Check AWS credentials"` | Key deactivated or mistyped. Create a new access key, update `.env`. |
| `not eligible for Free Tier` | Free-plan account: set `INSTANCE_TYPE=m7i-flex.large` (setup.sh normally does this for you). |
| Terraform `AccessDenied` / `UnauthorizedOperation` | The IAM user needs EC2, IAM (roles, OIDC provider), Budgets and S3. |
| `cannot SSH` / Kubernetes API unreachable | Your public IP changed. Re-run `scripts/setup.sh` (it updates the security group). Networks that rotate between several public IPs are detected and every one is allowed as a `/32`; you can also list them in `MY_IP=a.b.c.d,e.f.g.h`. |
| `connection refused` on port 6443 | Your network blocks outbound 6443. `setup.sh` detects this and switches kubectl to an SSH tunnel (`127.0.0.1:16443`); after a reboot run `scripts/tunnel.sh start`. |
| State lock error | Another run is in progress, or one was killed: `terraform -chdir=infra/terraform force-unlock <ID>`. |
| `ImagePullBackOff` with `IMAGE_SOURCE=ghcr` | The image is private or not built yet: set `GHCR_TOKEN`, or use `IMAGE_SOURCE=local`. |
| Gate fails with only *value date changed* | Someone changed `SETTLEMENT_TZ`; that is a real money change. |
| Teardown says resources are still billing | `scripts/teardown.sh --force-cleanup`. |

## Security notes

* `.env`, `.secrets/` (SSH key, kubeconfig, generated passwords) and Terraform state never enter git.
* CI uses GitHub OIDC: short-lived AWS credentials per run, a read-only plan role for PRs, and an apply role
  only the protected `production` environment can assume.
* SSH (22) and the Kubernetes API (6443) are open only to your public IP(s), each as an exact `/32`. IMDSv2 is enforced and EBS is encrypted.
* Containers run as non-root with a read-only root filesystem and all capabilities dropped.
* If AWS keys were ever pasted into a chat, ticket or log, **rotate them**: create a new access key in IAM,
  update `.env`, delete the old key.
