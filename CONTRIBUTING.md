# Contributing to AgentNet

Thank you for your interest in contributing to AgentNet!

## Table of Contents
- [Code of Conduct](#code-of-conduct)
- [Getting Started](#getting-started)
- [Development Environment](#development-environment)
- [Making Changes](#making-changes)
- [Testing](#testing)
- [Submitting Changes](#submitting-changes)
- [Style Guidelines](#style-guidelines)

## Code of Conduct

By participating in this project, you agree to abide by our [Code of Conduct](CODE_OF_CONDUCT.md). Please be respectful and inclusive.

## Getting Started

### Fork the Repository

```bash
# Fork via GitHub UI, then clone your fork
git clone https://github.com/YOUR_USERNAME/agentnet.git
cd agentnet

# Add upstream remote
git remote add upstream https://github.com/your-org/agentnet.git
```

### Understand the Project

- Read the [README.md](README.md)
- Review the [Architecture Documentation](docs/architecture.md)
- Explore the codebase structure

## Development Environment

### Prerequisites

- Python 3.10+
- Docker Desktop
- PostgreSQL 15 (for local development)
- Redis 7 (for local development)

### Setup Development Environment

```bash
# Create virtual environment
python -m venv venv
source venv/bin/activate  # Linux/Mac
# or: venv\Scripts\activate  # Windows

# Install dependencies
pip install -r services/registry/requirements.txt
pip install -r services/payment/requirements.txt

# Install dev dependencies
pip install pytest pytest-cov black flake8

# Copy environment file
cp .env.example .env

# Start local databases (or use Docker)
docker run -d -p 5432:5432 -e POSTGRES_PASSWORD=dev postgres:15
docker run -d -p 6379:6379 redis:7
```

### Run Tests Locally

```bash
# All tests
pytest

# Specific service
pytest tests/test_task_contract.py -v

# With coverage
pytest --cov=services --cov-report=html
```

## Making Changes

### Branch Naming

Use descriptive branch names:

```bash
# Feature branches
feature/add-new-agent-capability
feature/improve-escrow-logic

# Bug fixes
fix/wallet-balance-not-updating
fix/auth-token-expiry

# Documentation
docs/api-reference-updates
docs/fix-typos
```

### Code Style

We use:

- **Black** for code formatting (line length: 100)
- **flake8** for linting
- **isort** for import sorting

```bash
# Format code
black services/ tests/ examples/

# Sort imports
isort services/ tests/ examples/

# Check linting
flake8 services/ tests/ examples/
```

### Commit Messages

Follow [Conventional Commits](https://www.conventionalcommits.org/):

```bash
# Examples
feat: add new agent verification endpoint
fix: resolve escrow not releasing on timeout
docs: update API reference for v1
refactor: simplify wallet balance calculation
test: add unit tests for approval workflow
```

### Writing Tests

```python
# Test file naming: test_<module>.py
# Test class naming: Test<ComponentOrFeature>

import pytest

def test_escrow_reservation():
    """Test that escrow is reserved on task creation."""
    # Arrange
    wallet = Wallet(balance_credits=100)

    # Act
    reserve_escrow(wallet, amount=10)

    # Assert
    assert wallet.reserved_credits == 10
    assert wallet.balance_credits == 90
```

## Testing

### Unit Tests

```bash
# Run all unit tests
pytest tests/ -v

# Run specific test file
pytest tests/test_task_contract.py -v

# Run with coverage
pytest --cov=services/registry/app --cov-report=html
```

### Integration Tests

```bash
# Start services
docker compose up -d

# Run integration tests
pytest tests/test_integration.py -v

# Run specific integration test
pytest tests/test_integration.py::TestIntegrationHappyPath::test_01_user_register_and_login -v
```

### End-to-End Tests

```bash
# Run demo script
python examples/demo_end_to_end.py
```

## Submitting Changes

### Pull Request Process

1. **Create a Feature Branch**
   ```bash
   git checkout -b feature/my-feature
   ```

2. **Make Changes and Commit**
   ```bash
   git add .
   git commit -m "feat: add my feature"
   ```

3. **Push to Fork**
   ```bash
   git push origin feature/my-feature
   ```

4. **Open Pull Request**
   - Fill out the PR template
   - Link related issues
   - Request reviews

5. **Respond to Feedback**
   - Make requested changes
   - Re-run tests

### PR Template

```markdown
## Description
Brief description of changes

## Type of Change
- [ ] Bug fix
- [ ] New feature
- [ ] Documentation update
- [ ] Refactoring

## Testing
- [ ] Unit tests pass
- [ ] Integration tests pass
- [ ] Manual testing done

## Checklist
- [ ] Code follows style guidelines
- [ ] Self-review completed
- [ ] Documentation updated (if needed)
- [ ] Tests added/updated
```

## Style Guidelines

### Python

```python
# Use type hints
def create_task(caller_id: str, callee_id: str, capability: str) -> Task:
    """Create a new task session.

    Args:
        caller_id: UUID of the calling agent
        callee_id: UUID of the receiving agent
        capability: Name of capability to invoke

    Returns:
        Created task session

    Raises:
        ValueError: If caller and callee are the same
    """
    if caller_id == callee_id:
        raise ValueError("Caller and callee must be different")
    # ...
```

### Naming Conventions

- **Functions/Methods**: `snake_case`
- **Classes**: `PascalCase`
- **Constants**: `UPPER_SNAKE_CASE`
- **Private methods**: `_leading_underscore`

### Documentation

- Docstrings for all public functions
- Inline comments for complex logic
- README updates for user-facing changes

## Financial Code Guidelines

When modifying escrow, wallet, or transaction code:

1. **Always write tests** that verify:
   - Balance is correctly updated
   - No double-spending
   - Idempotent operations

2. **Document invariants**:
   ```python
   # INVARIANT: balance_credits + reserved_credits is always constant
   # for a wallet (funds are either available or locked)
   ```

3. **Use database triggers** for critical balance updates:
   - Don't update balances in application code
   - Use triggers to ensure consistency

## Questions?

- **Issues**: Open a GitHub issue
- **Discussions**: Use GitHub Discussions
- **Slack**: Join our community (link in README)

Thank you for contributing!
