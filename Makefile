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
	@echo "Installing dependencies..."
	cd services/registry && pip install -r requirements.txt
	cd services/payment && pip install -r requirements.txt
	cd services/worker && pip install -r requirements.txt
	cd services/dashboard && pip install -r requirements.txt
	cd sdk/python && pip install -e .
	pip install pytest pytest-cov black isort flake8

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

compose-validate:
	@echo "Validating docker-compose..."
	docker compose config

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
