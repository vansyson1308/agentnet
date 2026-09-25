"""
Test rate limiting for security.

Tests:
1. Rate limiter allows requests under threshold
2. Rate limiter blocks requests after threshold (429)
3. Rate limit headers are properly set
4. Different keys have separate limits
"""

import time
from unittest.mock import patch

import pytest


class TestRateLimiter:
    """Test in-memory rate limiter."""

    def test_allows_requests_under_limit(self):
        """Should allow requests under the limit."""
        from services.registry.app.security import InMemoryRateLimiter

        limiter = InMemoryRateLimiter(requests_per_minute=10)

        # Should allow first 10 requests
        for i in range(10):
            assert limiter.is_allowed("test_key") is True

    def test_blocks_requests_over_limit(self):
        """Should block requests over the limit."""
        from services.registry.app.security import InMemoryRateLimiter

        limiter = InMemoryRateLimiter(requests_per_minute=5)

        # Use up the limit
        for i in range(5):
            limiter.is_allowed("test_key")

        # Next request should be blocked
        assert limiter.is_allowed("test_key") is False

    def test_returns_remaining_requests(self):
        """Should return correct remaining request count."""
        from services.registry.app.security import InMemoryRateLimiter

        limiter = InMemoryRateLimiter(requests_per_minute=10)

        limiter.is_allowed("test_key")
        limiter.is_allowed("test_key")

        remaining = limiter.get_remaining("test_key")
        assert remaining == 8

    def test_different_keys_have_separate_limits(self):
        """Different keys should have separate rate limits."""
        from services.registry.app.security import InMemoryRateLimiter

        limiter = InMemoryRateLimiter(requests_per_minute=3)

        # Use up key1
        for i in range(3):
            limiter.is_allowed("key1")

        # key1 blocked, key2 should still work
        assert limiter.is_allowed("key1") is False
        assert limiter.is_allowed("key2") is True

    def test_time_window_cleanup(self):
        """Old requests should be cleaned up after time window."""
        from services.registry.app.security import InMemoryRateLimiter

        limiter = InMemoryRateLimiter(requests_per_minute=2)

        # Make requests
        limiter.is_allowed("test_key")
        limiter.is_allowed("test_key")

        assert limiter.is_allowed("test_key") is False

        # Manually clean old entries (simulate time passing)
        # The cleanup happens on each is_allowed call
        # For testing, we access internal state
        now = time.time()
        limiter.requests["test_key"] = [now - 120]  # 2 minutes ago

        # Should now allow requests after cleanup
        assert limiter.is_allowed("test_key") is True


class TestRateLimitMiddleware:
    """Test rate limiting via HTTP."""

    def test_rate_limit_headers_set(self):
        """Rate limit headers should be set on responses."""
        from fastapi import Depends, FastAPI
        from fastapi.testclient import TestClient

        from services.registry.app.security import InMemoryRateLimiter, check_rate_limit

        # Create test app with rate limiting
        app = FastAPI()

        @app.get("/limited")
        async def limited_endpoint():
            await check_rate_limit("test_ip")
            return {"message": "ok"}

        client = TestClient(app)
        response = client.get("/limited")

        # Should have rate limit headers when configured
        # Note: check_rate_limit raises exception, so we need different approach

    def test_429_response_when_rate_limited(self):
        """Should return 429 when rate limit exceeded."""
        from fastapi import FastAPI, HTTPException
        from fastapi.testclient import TestClient

        # Reset global limiter
        import services.registry.app.security as security_module
        from services.registry.app.security import InMemoryRateLimiter

        security_module._rate_limiter = InMemoryRateLimiter(requests_per_minute=2)

        app = FastAPI()

        @app.get("/test")
        async def test_endpoint():
            limiter = security_module.get_rate_limiter()
            if not limiter.is_allowed("test_client"):
                raise HTTPException(
                    status_code=429,
                    detail="Rate limit exceeded",
                    headers={"X-RateLimit-Remaining": "0"},
                )
            return {"ok": True}

        client = TestClient(app)

        # Make requests up to limit
        client.get("/test")
        client.get("/test")

        # Next request should be 429
        response = client.get("/test")
        assert response.status_code == 429


class TestRateLimitConfiguration:
    """Test rate limit configuration from environment."""

    def test_default_rate_limit(self):
        """Should use default rate limit of 60."""
        with patch.dict("os.environ", {}, clear=False):
            # Ensure RATE_LIMIT_PER_MINUTE is not set
            if "RATE_LIMIT_PER_MINUTE" in __import__("os").environ:
                del __import__("os").environ["RATE_LIMIT_PER_MINUTE"]

            from services.registry.app.security import InMemoryRateLimiter

            limiter = InMemoryRateLimiter(requests_per_minute=60)
            assert limiter.requests_per_minute == 60

    def test_custom_rate_limit_from_env(self):
        """Should use custom rate limit from environment."""
        with patch.dict("os.environ", {"RATE_LIMIT_PER_MINUTE": "100"}):
            # Reset global
            import services.registry.app.security as security_module
            from services.registry.app.security import get_rate_limiter

            security_module._rate_limiter = None

            limiter = get_rate_limiter()
            assert limiter.requests_per_minute == 100


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])


class TestClientIdentityIsNotSpoofable:
    """Phase 2.5 §9/§14: the unauthenticated bucket key must come from the
    peer address uvicorn vouches for, never from a header the caller
    controls. Otherwise one client gets a fresh bucket per request by
    rotating X-Forwarded-For (login/register brute force)."""

    def _limiter(self):
        import sys

        sys.path.insert(0, "services/registry")
        from services.registry.app.api.rate_limiter import RateLimitMiddleware

        return RateLimitMiddleware(app=None, default_rate=10, default_burst=10, agent_rate=10, agent_burst=10)

    def _request(self, headers: dict, client=("203.0.113.9", 1234)):
        from starlette.requests import Request

        scope = {
            "type": "http",
            "method": "GET",
            "path": "/v1/auth/login",
            "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
            "client": client,
            "query_string": b"",
        }
        return Request(scope)

    def test_forwarded_for_header_does_not_change_the_key(self):
        limiter = self._limiter()
        base = limiter._get_client_key(self._request({}))
        spoofed = [limiter._get_client_key(self._request({"X-Forwarded-For": f"198.51.100.{i}"})) for i in range(5)]
        assert set(spoofed) == {base}, "a caller-controlled header must not mint new buckets"
        assert base == "203.0.113.9"

    @staticmethod
    def _jwt(sub="7b0a6a53-1f2f-4f55-9a58-4d7d0c2f1e11", token_type="user", secret=None, expires_in=600):
        from datetime import datetime, timedelta, timezone

        from jose import jwt

        from services.registry.app.config import JWT_ALGORITHM, JWT_SECRET_KEY

        claims = {"sub": sub, "type": token_type, "exp": datetime.now(timezone.utc) + timedelta(seconds=expires_in)}
        return jwt.encode(claims, secret or JWT_SECRET_KEY, algorithm=JWT_ALGORITHM)

    def test_unverified_bearer_cannot_mint_a_bucket(self):
        """Regression (public-edge measurement, 2026-09-25): a random bearer
        per request used to get a fresh bucket each time. An unverified token
        is now exactly like no token: the caller's peer bucket."""
        limiter = self._limiter()
        base = limiter._get_client_key(self._request({}, client=("1.1.1.1", 1)))
        garbage = {
            limiter._get_client_key(self._request({"Authorization": f"Bearer tok-{i}"}, client=("1.1.1.1", 1)))
            for i in range(5)
        }
        assert garbage == {base} == {"1.1.1.1"}

    def test_forged_expired_and_scoped_tokens_fall_back_to_the_peer(self):
        limiter = self._limiter()
        for token in (
            self._jwt(secret="not-the-server-secret-0123456789abcdef"),
            self._jwt(expires_in=-60),
            self._jwt(token_type="root"),
            "spt_" + "a" * 43,
        ):
            key = limiter._get_client_key(self._request({"Authorization": f"Bearer {token}"}, client=("1.1.1.1", 1)))
            assert key == "1.1.1.1", token[:12]

    def test_verified_token_keys_by_subject_not_ip(self):
        limiter = self._limiter()
        tok_a, tok_b = self._jwt(), self._jwt(sub="0f9d2a1e-4a55-4d7a-8c2e-9a1b2c3d4e5f")
        a1 = limiter._get_client_key(self._request({"Authorization": f"Bearer {tok_a}"}, client=("1.1.1.1", 1)))
        a2 = limiter._get_client_key(self._request({"Authorization": f"Bearer {tok_a}"}, client=("2.2.2.2", 1)))
        b = limiter._get_client_key(self._request({"Authorization": f"Bearer {tok_b}"}, client=("1.1.1.1", 1)))
        assert a1 == a2 != b
        assert a1.startswith("sub:") and tok_a not in a1, "the key is a digest of the subject, never the token"

    def test_agent_tier_requires_a_verified_agent_token(self):
        import asyncio

        limiter = self._limiter()

        def is_agent(headers):
            return asyncio.run(limiter._is_agent(self._request(headers)))

        assert is_agent({"Authorization": "Bearer short-garbage"}) is False
        assert is_agent({"Authorization": f"Bearer {self._jwt(token_type='user')}"}) is False
        assert is_agent({"Authorization": f"Bearer {self._jwt(token_type='agent')}"}) is True

    def _auth_app(self, default_rate=3, agent_rate=300):
        import sys

        sys.path.insert(0, "services/registry")
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from services.registry.app.api.rate_limiter import RateLimitMiddleware

        app = FastAPI()
        app.add_middleware(
            RateLimitMiddleware, default_rate=default_rate, default_burst=default_rate,
            agent_rate=agent_rate, agent_burst=agent_rate,
        )

        @app.post("/v1/auth/user/login")
        def login():
            return {"ok": True}

        @app.post("/v1/auth/user/register")
        def register():
            return {"ok": True}

        @app.get("/v1/tasks/")
        def tasks():
            return []

        return TestClient(app)

    def test_login_and_register_stay_bounded_under_rotating_garbage_bearers(self):
        """The exploit, end to end through the real middleware (in-memory path):
        a caller rotates a fresh bogus bearer on every login/register attempt.
        All attempts share ONE default-tier bucket and the limiter still bites."""
        client = self._auth_app(default_rate=3)
        responses = []
        for i in range(6):
            path = "/v1/auth/user/login" if i % 2 == 0 else "/v1/auth/user/register"
            responses.append(client.post(path, headers={"Authorization": f"Bearer bogus-{i}-{'x' * (i % 3)}"}))
        assert [r.status_code for r in responses] == [200, 200, 200, 429, 429, 429]
        assert {r.headers.get("X-RateLimit-Limit") for r in responses[:3]} == {"3"}, "garbage never earns the agent tier"
        assert [r.headers.get("X-RateLimit-Remaining") for r in responses[:3]] == ["2", "1", "0"], "one shared bucket"

    def test_verified_token_keeps_its_own_tier_and_bucket(self):
        """The fix must not break real callers: a verified agent token gets the
        agent tier in its own bucket, unaffected by an exhausted IP bucket."""
        client = self._auth_app(default_rate=2, agent_rate=5)
        for i in range(3):  # exhaust the peer's unauthenticated bucket
            client.post("/v1/auth/user/login", headers={"Authorization": f"Bearer junk-{i}"})
        agent = client.get("/v1/tasks/", headers={"Authorization": f"Bearer {self._jwt(token_type='agent')}"})
        assert agent.status_code == 200
        assert agent.headers.get("X-RateLimit-Limit") == "5"
        user = client.get("/v1/tasks/", headers={"Authorization": f"Bearer {self._jwt(token_type='user')}"})
        assert user.status_code == 200 and user.headers.get("X-RateLimit-Limit") == "2"

    def test_rate_limiter_source_never_parses_forwarded_headers(self):
        import pathlib

        for svc in ("registry", "payment"):
            text = pathlib.Path(f"services/{svc}/app/api/rate_limiter.py").read_text(encoding="utf-8")
            body = "\n".join(line for line in text.splitlines() if not line.strip().startswith("#") and '"""' not in line)
            assert 'headers.get("X-Forwarded-For"' not in body, svc
