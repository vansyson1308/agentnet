# AgentNet Makefile
# Common development tasks

.PHONY: help install lint format test test-ci compose-up compose-down compose-logs clean

# ─────────────────────────────────────────────────────────
# Setup
# ─────────────────────────────────────────────────────────

help:
	@echo "AgentNet Development Commands"
	@echo ""
	@echo "  make install        Install dependencies"
	@echo "  make lint           Run linters"
	@echo "  make format         Format code"
	@echo "  make test           Run unit tests"
	@echo "  make test-ci        Run tests with coverage"
	@echo "  make compose-up     Start all services"
	@echo "  make compose-down   Stop all services"
	@echo "  make compose-logs   View logs"
	@echo "  make clean          Clean up containers and volumes"
	@echo "  make demo          Run end-to-end demo"

install:
	@echo "Installing dependencies (one resolver pass over every service's pins + test tooling)..."
	pip install -r requirements-dev.txt
	pip check
	cd sdk/python && pip install -e .

# ─────────────────────────────────────────────────────────
# Lint & Format
# ─────────────────────────────────────────────────────────

lint:
	@echo "Running linters..."
	flake8 services/ tests/ sdk/ --max-line-length=120 --ignore=E501,W503
	isort --check-only services/ tests/ sdk/
	black --check services/ tests/ sdk/

format:
	@echo "Formatting code..."
	black services/ tests/ sdk/
	isort services/ tests/ sdk/

# ─────────────────────────────────────────────────────────
# Tests
# ─────────────────────────────────────────────────────────

test:
	@echo "Running unit tests..."
	pytest tests/ --ignore=tests/test_integration.py -v

test-ci:
	@echo "Running tests with coverage..."
	pytest tests/ --ignore=tests/test_integration.py --cov=services --cov-report=term-missing -v

# ─────────────────────────────────────────────────────────
# Docker Compose
# ─────────────────────────────────────────────────────────

# Throwaway values so the staging project renders. Generated per run so no
# credential-shaped literal ever lives in the repository (secret scanners
# rightly flag those); they are never used against a real service.
RENDER_ENV = POSTGRES_HOST=db.invalid POSTGRES_USER=render POSTGRES_PASSWORD=$$(openssl rand -hex 8) \
	REDIS_HOST=redis.invalid REDIS_PASSWORD=$$(openssl rand -hex 8) JWT_SECRET_KEY=$$(openssl rand -hex 16) \
	FLASK_SECRET_KEY=$$(openssl rand -hex 16) CORS_ALLOWED_ORIGINS=https://staging.invalid

compose-validate:
	@echo "Validating LOCAL compose project (agentnet-local)..."
	docker compose -f docker-compose.yml config > /dev/null
	@echo "Validating STAGING compose project (agentnet-staging; throwaway env, render only)..."
	$(RENDER_ENV) docker compose -f docker-compose.staging.yml config > /dev/null
	@echo "Validating STAGING + shared-infra overlay..."
	SHARED_INFRA_NETWORK=render-overlay $(RENDER_ENV) docker compose -f docker-compose.staging.yml -f docker-compose.staging.shared-infra.yml config > /dev/null
	@echo "compose projects OK (local + staging are separate projects; see tests/test_compose_topology.py)"

compose-staging-config:
	docker compose -f docker-compose.staging.yml config

compose-up:
	@echo "Starting services..."
	docker compose up -d --build

compose-down:
	@echo "Stopping services..."
	docker compose down

compose-logs:
	docker compose logs -f

compose-logs-registry:
	docker compose logs -f registry

compose-logs-payment:
	docker compose logs -f payment

compose-logs-worker:
	docker compose logs -f worker

compose-logs-dashboard:
	docker compose logs -f dashboard

# ─────────────────────────────────────────────────────────
# Cleanup
# ─────────────────────────────────────────────────────────

clean:
	@echo "Cleaning up..."
	docker compose down -v
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete 2>/dev/null || true
	find . -type d -name ".pytest_cache" -exec rm -rf {} + 2>/dev/null || true

# ─────────────────────────────────────────────────────────
# Demo
# ─────────────────────────────────────────────────────────

demo:
	@echo "Running end-to-end demo..."
	@echo "Make sure services are running first: make compose-up"
	python examples/demo_end_to_end.py

# ─────────────────────────────────────────────────────────
# Release
# ─────────────────────────────────────────────────────────

release-dry-run:
	@echo "Dry run release - validating..."
	@echo "Version: $$(cat VERSION)"
	docker compose build

release:
	@echo "Building release images..."
	@echo "Version: $$(cat VERSION)"
	docker build -t agentnet/registry:$$(cat VERSION) ./services/registry
	docker build -t agentnet/payment:$$(cat VERSION) ./services/payment
	docker build -t agentnet/worker:$$(cat VERSION) ./services/worker
	docker build -t agentnet/dashboard:$$(cat VERSION) ./services/dashboard
