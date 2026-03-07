# Release Process

This document outlines the release process for AgentNet.

## Versioning Strategy

AgentNet uses **Semantic Versioning (SemVer)**:

- `MAJOR`: Breaking changes (incompatible API changes)
- `MINOR`: New features (backward-compatible)
- `PATCH`: Bug fixes (backward-compatible)

Version format: `MAJOR.MINOR.PATCH`

Examples:
- `0.1.0` - First stable release
- `0.2.0` - New features added
- `0.2.1` - Bug fix
- `1.0.0` - First production release
- `1.1.0` - New features (backward-compatible)
- `2.0.0` - Breaking changes

## Pre-Release Checklist

### 1. Code Quality

```bash
# All unit tests pass
pytest tests/ -v

# Linting passes
flake8 services/ tests/
black --check services/ tests/
docker compose config
```

### 2. Integration Tests

```bash
# Services start successfully
docker compose up -d --build

# Health checks pass
curl http://localhost:8000/health
curl http://localhost:8001/health

# Demo runs end-to-end
python examples/demo_end_to_end.py
```

### 3. Documentation

- [ ] CHANGELOG.md updated with changes
- [ ] README.md reflects current state
- [ ] API documentation is accurate
- [ ] Deployment guide is up-to-date

### 4. Security

- [ ] No secrets in code (check for hardcoded passwords)
- [ ] CORS configured appropriately
- [ ] Rate limiting enabled
- [ ] JWT secrets are strong

## Release Steps

### Manual Release

```bash
# 1. Update version
echo "0.1.0" > VERSION

# 2. Update CHANGELOG.md
# Add release date and changes

# 3. Run pre-release checks
docker compose config
pytest tests/ -v

# 4. Commit changes
git add VERSION CHANGELOG.md
git commit -m "Release v0.1.0"

# 5. Tag release
git tag -a v0.1.0 -m "Release v0.1.0"

# 6. Push
git push && git push --tags
```

### CI Release (GitHub Actions)

1. Push to `main` branch triggers CI pipeline
2. CI runs:
   - Linting
   - Unit tests
   - Docker validation
3. On success, images are built
4. Create GitHub release with tag

## Docker Image Tags

| Service | Tag | Description |
|---------|-----|-------------|
| registry | `latest`, `0.1.0` | Registry service |
| payment | `latest`, `0.1.0` | Payment service |
| worker | `latest`, `0.1.0` | Background worker |
| dashboard | `latest`, `0.1.0` | Dashboard UI |

## Rollback

If a release has critical issues:

```bash
# View available tags
git tag

# Checkout previous version
git checkout v0.0.1

# Or revert the commit
git revert HEAD

# Rebuild
docker compose up -d --build
```

## Post-Release

1. Update version in VERSION file to next dev version
2. Announce release (if applicable)
3. Update dependent projects

## Quick Reference

```bash
# Development version
echo "0.2.0-dev" > VERSION

# Release version
echo "0.1.0" > VERSION
```
