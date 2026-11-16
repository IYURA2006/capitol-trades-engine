SHELL := /bin/bash
GOBIN := $(shell go env GOPATH)/bin
PY    ?= .venv/bin/python
N     ?= 100
SEED  ?= 1
WORKFLOW ?= capitol
SNAPSHOT ?= full

# Port 5433 on the host, so a local Postgres on 5432 does not get in the way.
export TEST_DATABASE_URL ?= postgres://engine:engine@localhost:5433/engine?sslmode=disable

.PHONY: up down db test test-go test-py test-integration chaos chaos-stack chaos-real demo proto lint research

up: ## start Postgres and two coordinators
	docker compose up -d --build --wait

down: ## stop everything and delete the data volume
	docker compose down -v --remove-orphans

db: ## start only Postgres (enough for the Go tests)
	docker compose up -d --wait postgres

test: test-go test-py

test-go: db
	cd coordinator && go test -count=1 ./...

test-py:
	$(PY) -m pytest -q

# Kill and freeze real worker containers. A short lease keeps it fast.
test-integration:
	LEASE=6s REAPER_INTERVAL=1s BACKOFF_BASE=200ms WORKERS=2 docker compose up -d --build --wait
	TEST_LEASE_S=6 $(PY) -m pytest -q -s -m integration tests/integration

# Chaos settings: a short lease so that faults are felt quickly.
chaos-stack:
	LEASE=5s REAPER_INTERVAL=1s BACKOFF_BASE=200ms WORKERS=6 docker compose up -d --build --wait
	cd chaos && go build -o ../bin/chaos .

chaos: chaos-stack ## N chaos runs of the toy workflow, each checked against a reference
	./bin/chaos run -n $(N) -seed $(SEED) -lease 5s -workflow toy \
		-param n=2000 -param max_sleep_ms=20 -ref-param max_sleep_ms=0

# The real pipeline, replayed from the full snapshot. The workflow seed is
# fixed, so one reference serves every run; the fault schedule still changes.
chaos-real:
	TASK_MODULES=pipeline.tasks.filings LEASE=5s REAPER_INTERVAL=1s BACKOFF_BASE=200ms WORKERS=6 \
		docker compose up -d --build --wait
	cd chaos && go build -o ../bin/chaos .
	./bin/chaos run -n $(N) -seed $(SEED) -lease 5s -timeout 30m -workflow $(WORKFLOW) -snapshot $(SNAPSHOT) \
		-modules pipeline.tasks.filings -workflow-seed 7 -rate house-clerk=20ms -rate prices=10ms

demo: ## the real pipeline on the committed sample, with faults, checked against a reference
	$(MAKE) chaos-real N=1 WORKFLOW=capitol_sample SNAPSHOT=sample
	@echo "status page: http://localhost:8081 and http://localhost:8082"

# The deeper finance checks, on the records of a finished run. RUN is the JSON
# written by `capitol_sdk.cli local --out` or by the chaos harness. Needs the
# full snapshot for the price files; reads no website.
RUN ?= $(lastword $(sort $(wildcard chaos/results/capitol-*.reference.json)))
research: ## research checks and the full report (out/report.html)
	$(PY) tools/run_research.py --json $(RUN) --out out/research.json
	$(PY) tools/make_report.py --json $(RUN) --research out/research.json --out out/report.html

proto: ## regenerate Go and Python code from proto/engine.proto
	PATH="$(GOBIN):$$PATH" protoc -I proto \
		--go_out=coordinator/internal/pb --go_opt=paths=source_relative \
		--go-grpc_out=coordinator/internal/pb --go-grpc_opt=paths=source_relative \
		proto/engine.proto
	$(PY) tools/gen_proto.py

lint:
	cd coordinator && go vet ./... && test -z "$$(gofmt -l .)"
	cd chaos && go vet ./... && test -z "$$(gofmt -l .)"
	$(PY) -m ruff check sdk pipeline tools tests
