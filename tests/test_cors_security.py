"""
Test CORS configuration for security.

Tests:
1. Random origin is disallowed in production mode
2. Localhost origins allowed in development mode
3. CORS headers properly set on responses
"""

import pytest
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient
from httpx import Response


class TestCORSConfiguration:
    """Test CORS security configuration."""

    def test_dev_mode_allows_localhost_origins(self):
        """Development mode should allow localhost origins."""
        with patch.dict("os.environ", {"ENVIRONMENT": "development"}):
            # Import after env patch to get dev origins
            from services.registry.app.security import get_cors_origins

            origins = get_cors_origins()

            assert "http://localhost:3000" in origins
            assert "http://localhost:8000" in origins
            assert "http://127.0.0.1:3000" in origins

    def test_prod_mode_requires_explicit_origins(self):
        """Production mode should require explicit CORS_ALLOWED_ORIGINS."""
        with patch.dict("os.environ", {"ENVIRONMENT": "production", "CORS_ALLOWED_ORIGINS": "https://app.example.com,https://admin.example.com"}):
            from services.registry.app.security import get_cors_origins

            origins = get_cors_origins()

            assert "https://app.example.com" in origins
            assert "https://admin.example.com" in origins
            assert "http://localhost:3000" not in origins

    def test_prod_mode_raises_without_config(self):
        """Production mode should raise error without CORS_ALLOWED_ORIGINS."""
        with patch.dict("os.environ", {"ENVIRONMENT": "production", "CORS_ALLOWED_ORIGINS": ""}):
            from services.registry.app.security import get_cors_origins

            with pytest.raises(ValueError, match="CORS_ALLOWED_ORIGINS must be set"):
                get_cors_origins()

    def test_random_origin_not_in_allowed_list(self):
        """Random/or suspicious origins should not be in allowed list."""
        with patch.dict("os.environ", {"ENVIRONMENT": "production", "CORS_ALLOWED_ORIGINS": "https://app.example.com"}):
            from services.registry.app.security import get_cors_origins

            origins = get_cors_origins()

            # These should never be allowed in production
            assert "http://evil.com" not in origins
            assert "http://attacker.io" not in origins
            assert "null" not in origins

    def test_cors_headers_on_response(self, tmp_path):
        """Responses should include security headers."""
        # Create minimal app to test middleware
        from fastapi import FastAPI
        from services.registry.app.security import setup_security_headers

        app = FastAPI()
        setup_security_headers(app)

        @app.get("/test")
        async def test_endpoint():
            return {"message": "test"}

        client = TestClient(app)
        response = client.get("/test")

        assert response.status_code == 200
        assert response.headers.get("X-Content-Type-Options") == "nosniff"
        assert response.headers.get("X-Frame-Options") == "DENY"
        assert "Strict-Transport-Security" in response.headers


class TestSecurityHeadersMiddleware:
    """Test security headers middleware."""

    def test_all_required_headers_present(self, tmp_path):
        """All required security headers should be present."""
        from fastapi import FastAPI, Request
        from fastapi.testclient import TestClient
        from services.registry.app.security import SecurityHeadersMiddleware

        app = FastAPI()
        app.add_middleware(SecurityHeadersMiddleware)

        @app.get("/")
        async def root():
            return {"ok": True}

        client = TestClient(app)
        response = client.get("/")

        # Check all security headers
        assert "X-Content-Type-Options" in response.headers
        assert "X-Frame-Options" in response.headers
        assert "X-XSS-Protection" in response.headers
        assert "Strict-Transport-Security" in response.headers

        # Verify values
        assert response.headers["X-Content-Type-Options"] == "nosniff"
        assert response.headers["X-Frame-Options"] == "DENY"
        assert response.headers["X-XSS-Protection"] == "1; mode=block"


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
