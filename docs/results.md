# Verified results (2026-10-31)

Everything below was run on the author's machine and on a real AWS account (Free plan, ap-south-1),
not just written. Re-run any of it with the commands shown.

## Tests

| Suite | Command | Result |
|---|---|---|
| Unit (money, API, rail, worker, tools, diff, AWS helper, shell library, repo consistency) | `make test` | **205 passed** |
| Integration against docker compose | `make up && make test-integration` | **9 passed** |
| Traffic reproducible from seed 42 | `python traffic/generator.py --check` | identical, 5,000 lines |
| shellcheck (warning level) on every script | see `ledger-gate.yml` unit job | clean |
| `helm lint` (CI and AWS values), `terraform fmt -check` + `validate` | `make lint` | clean |

## Determinism

Two fresh docker compose stacks, full replay + export each: **byte-identical** 38,764-row CSVs.
The same traffic on Kubernetes (kind, the CI path) produced a ledger **identical to the baseline recorded on
docker compose**: the ledger does not depend on where it runs.

## Gate 1: before merge

| Scenario | Environment | Verdict |
|---|---|---|
| No change | docker compose | PASS: 38,764 rows identical |
| No change | kind + Helm (exact CI path, `scripts/kind-gate.sh`) | PASS: 38,764 rows identical |
| `ROUNDING_MODE=ROUND_HALF_UP` | docker compose | FAIL: INR 339 / MXN 67 / BRL 48 payments, *amount changed*, invariant still holds |
| `SETTLEMENT_TZ=Asia/Kolkata` | docker compose | FAIL: 1,246 payments, *value date changed* |
| Worker timeout 100 ms < rail latency 300 ms | docker compose | FAIL: 4,850 *duplicate payouts* (each paid 5× at the rail) |
| Rail failure rate 5‰ → 50‰ | docker compose | FAIL: 225 payments *status changed*, payout legs *missing* |

## Gate 1 on GitHub: the demo pull requests

Each PR ran `unit-tests` and `ledger-gate` (required checks on `main`) on GitHub-hosted runners: kind cluster,
Helm chart, full replay of 5,000 payments, diff against the baseline.

| PR | Change | ledger-gate | Outcome |
|---|---|---|---|
| [#1](https://github.com/Akshith1413/ProofOfBalance/pull/1) | Postgres 16 → 17 | ✅ 38,764 rows identical | merged |
| [#2](https://github.com/Akshith1413/ProofOfBalance/pull/2) | `ROUND_HALF_UP` | ❌ INR 339 / MXN 67 / BRL 48 payments, *amount changed* | closed |
| [#3](https://github.com/Akshith1413/ProofOfBalance/pull/3) | `SETTLEMENT_TZ=Asia/Kolkata` | ❌ INR 618 / MXN 381 / BRL 247 payments, *value date changed* | closed |
| [#4](https://github.com/Akshith1413/ProofOfBalance/pull/4) | 4 worker replicas | ✅ identical (passed twice: branch was updated after #1 merged) | merged |
| [#5](https://github.com/Akshith1413/ProofOfBalance/pull/5) | no `SKIP LOCKED` + 4 workers | ❌ 4,834 payments paid 2–6× at the rail; unit tests green, ledger balanced | closed |
| [#6](https://github.com/Akshith1413/ProofOfBalance/pull/6) | rail timeout 2000 → 100 ms | ✅ identical (CI's rail answers instantly) | merged, then rolled back on AWS (below) |

The kind results match the docker compose results above exactly (454 and 1,246 payments).

## Gate 2: in production (AWS)

Environment created by `scripts/setup.sh`: m7i-flex.large (Free-plan account), k3s v1.36,
kube-prometheus-stack, Argo Rollouts, ProofOfBalance with 5 Rollout workers and live trickle traffic.

| Exercise | What happened | Verdict |
|---|---|---|
| Smoke test | `helm test` passed; a payment over the API was paid with 7 balanced postings | ✔ |
| Second `setup.sh` run | Terraform "No changes", images "already on the node", smoke test passed | ✔ idempotent |
| Healthy release (new image) | 20% canary → AnalysisRun **Successful** (failed-value-ratio 0 ×5, duplicates 0 ×5) → 60% → pause → **100%, Healthy** | ✔ promoted |
| Bad release (`worker.railTimeoutMs=100`, `rail.delayMs=300`) | canary paid 77 transfers twice within ~80 s; `no-duplicate-payouts` failed → **RolloutAborted**, canary scaled to 0, stable kept at 100%; **DuplicatePayout** alert *firing* (critical) | ✔ rolled back |
| Ledger invariant during all of the above | `pob_ledger_unbalanced_payments` = 0 | ✔ |
| Prometheus | 8 ProofOfBalance targets up; 5 alert rules loaded; metrics carry `rollouts_pod_template_hash` | ✔ |
| `teardown.sh --pause`, then `setup.sh` | instance stopped (verified via EC2 API), then started again; data and images kept; smoke test passed | ✔ |
| `teardown.sh` (destroy) | 28 resources destroyed in ~70 s; tag scan: **0 resources left**; state bucket kept | ✔ |
| `setup.sh` from scratch after destroy | one run, no manual steps: 28 resources, k3s, monitoring, Argo, app, smoke payment paid, in ~10 min | ✔ |

### Second round: clean clone, demo PR 6, alerts (same day)

Run from a fresh `git clone` of the GitHub repository with only `.env` copied in (no `.secrets/`, `.tools/`, `.venv/`).

| Exercise | What happened | Verdict |
|---|---|---|
| `teardown.sh` from the clean clone | no local tools yet: installed them, read the S3 state, destroyed 28 resources, tag scan found 0 left | ✔ |
| `setup.sh` from the clean clone | 28 resources created, then failed at *Wait for k3s* with a clear status file: the SSH control socket path was too long for a deeply nested clone (bug, fixed and tested) | ✔ failure explained |
| `setup.sh` re-run after the fix | Terraform "0 added, 0 changed", tunnel up, Postgres **17** fresh, MXN rail delay 300 ms, smoke payment paid, GitHub variables set, budget emails configured | ✔ resumed |
| Demo PR 6 deployed (`railTimeoutMs` 100) | canary 1 of 5 workers; first analysis: `failed-value-ratio` **1.0** (MXN, limit 0.005, one strike tolerated), `no-duplicate-payouts` **16.6** (limit 0) → **RolloutAborted ~1 min after the canary started**, canary scaled to 0, stable kept serving, `pob_ledger_unbalanced_payments` 0 | ✔ rolled back |
| `scripts/test-alerts.sh` (no webhook configured) | in-cluster receiver got both the test alert **and the real `DuplicatePayout` alert from PR 6**, in Discord (`embeds`) and Slack (`attachments`) formats; Alertmanager config rolled back afterwards | ✔ delivered |
| Grafana | read-only anonymous viewer through `port-forward` (no password handled); canary-vs-stable panel shows the canary's MXN line at 100% | ✔ |
| Budget email in public plan output | `terraform plan` shows the notification blocks as *sensitive*, the address is not printed | ✔ |

## Problems found while running it for real (and fixed)

These only showed up by actually running the scripts end to end; each is now handled automatically and
covered by a test or a validation where possible.

| Problem | Symptom | Fix |
|---|---|---|
| AWS **Free-plan** account refuses non-Free-Tier instance types | `InvalidParameterCombination: not eligible for Free Tier` (EC2 DryRun does not detect it) | `setup.sh` reads the account plan (Free Tier API) and picks `m7i-flex.large`; Terraform sets CPU credits only for `t*` types |
| Network rotates between **4 public IPs** | SSH worked, then timed out | every egress IP is detected and allowed as its own `/32` (`extra_admin_ips`) |
| Network **blocks outbound 6443** | `connection refused` to the k3s API | automatic SSH tunnel (`127.0.0.1:16443`), `scripts/tunnel.sh` |
| VPC `10.42.0.0/16` overlapped **k3s pod CIDR** | CoreDNS never ready, pods time out to 10.43.0.1 | VPC `10.20.0.0/16`, variable validation rejects overlaps |
| `--node-external-ip` made k3s advertise the **public IP** in-cluster | pods hairpinned into the locked-down security group | flag removed (`--tls-san` is enough); CoreDNS readiness check added |
| VPC replacement deadlocked on the internet gateway | `DependencyViolation` for 8+ minutes | `replace_triggered_by` on the gateway |
| `tr </dev/urandom \| head` under `pipefail` | setup exited 141 (SIGPIPE) | Python `secrets`; ERR trap now names the failing command and line |
| Empty `POSTGRES_PASSWORD=` in `.env` blocked the saved generated one | new password every run; guard refused to deploy | loader precedence fixed; password change now **rotated in place** (no data loss) |
| Generated secrets file became world-readable | `generated.env` mode 644 | written under `umask 077` (test added) |
| Docker Desktop image ids change on every build | images re-uploaded every run | tag by source-content hash |
| `kind load` of multi-platform `postgres:16` | `content digest not found` | side-load is best-effort; cluster pulls it otherwise |
| SSH control socket inside the checkout | `path too long for Unix domain socket` from a clone in a long path | socket in `$XDG_RUNTIME_DIR/pob-$UID/`, keyed by a hash of the checkout (test added) |
| `teardown.sh` on a fresh clone | said "kind gate clusters stopped" without checking (kind not installed yet) | kind clusters found by container label; reports what it removed |
| Canary that times out but never fails *terminally* | its retries were paid by stable pods, so failed value looked like 0 | timed-out attempts count as `retry` value against the pod that made them; analysis takes the worst corridor |
| Budget email in Terraform plan | would be printed in public PR comments | variable is `sensitive` |
| `helm test --logs` after hook deletion | "pod not found" although the test passed | test pod kept until the next run |
