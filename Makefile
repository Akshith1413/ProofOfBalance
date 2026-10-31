# Shortcuts. Every target is a thin wrapper around a script you can run directly.
PY ?= .venv/bin/python

.PHONY: help venv test test-integration traffic up down gate gate-kind baseline aws-up aws-down aws-pause aws-test-alerts lint

help:            ## list targets
	@grep -E '^[a-z-]+:.*##' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-17s %s\n", $$1, $$2}'

venv:            ## create .venv with every Python dependency
	python3 -m venv .venv && .venv/bin/pip install -q -r requirements-dev.txt -r scripts/requirements.txt

test:            ## unit tests (no Docker needed)
	$(PY) -m pytest tests/unit

test-integration: ## integration tests against the running compose stack
	$(PY) -m pytest -m integration tests/integration

traffic:         ## verify traffic/day1.jsonl regenerates byte-for-byte
	$(PY) traffic/generator.py --check

up:              ## local stack (docker compose) + smoke test
	scripts/setup.sh --local

down:            ## stop local stacks
	scripts/teardown.sh --local

gate:            ## ledger gate on docker compose (pass ARGS="ROUNDING_MODE=ROUND_HALF_UP")
	scripts/local-gate.sh $(ARGS)

gate-kind:       ## the exact CI gate on a local kind cluster (ARGS="--set settlement.timezone=Asia/Kolkata")
	scripts/kind-gate.sh $(ARGS)

baseline:        ## re-record baseline/ledger_baseline.csv (commit it in a PR labelled update-baseline)
	scripts/local-gate.sh --update-baseline

lint:            ## helm lint + terraform fmt/validate + bash -n
	.tools/bin/helm lint helm/proofofbalance -f helm/proofofbalance/values-ci.yaml
	.tools/bin/terraform -chdir=infra/terraform fmt -check -recursive
	for f in scripts/*.sh scripts/lib/*.sh scripts/ci/*.sh db/migrate.sh; do bash -n $$f; done

aws-up:          ## create/repair the AWS environment (reads .env)
	scripts/setup.sh

aws-pause:       ## stop the EC2 instance (keeps everything else)
	scripts/teardown.sh --pause

aws-down:        ## destroy all AWS resources and stop local stacks
	scripts/teardown.sh

aws-test-alerts: ## prove alerts leave the cluster (Discord/Slack, or an in-cluster receiver)
	scripts/test-alerts.sh
