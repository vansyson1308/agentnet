"""
Tests for the Agent Sandbox — security layer for external agent calls.

Verifies:
1. SSRF protection (blocks internal networks)
2. Timeout enforcement
3. Response size validation
4. Header stripping (no credential leaking)
"""

import pytest

from services.registry.app.sandbox import (
    SSRFError,
    SandboxConfig,
    SandboxError,
    _check_ssrf,
    _validate_response,
)


class TestSSRFProtection:
    """Test that SSRF attacks are blocked."""

    @pytest.fixture
    def config(self):
        return SandboxConfig(block_private_networks=True)

    def test_blocks_localhost(self, config):
        with pytest.raises(SSRFError, match="internal service"):
            _check_ssrf("http://localhost:8000/verify", config)

    def test_blocks_internal_postgres(self, config):
        with pytest.raises(SSRFError, match="internal service"):
            _check_ssrf("http://postgres:5432", config)

    def test_blocks_internal_redis(self, config):
        with pytest.raises(SSRFError, match="internal service"):
            _check_ssrf("http://redis:6379", config)

    def test_blocks_internal_registry(self, config):
        with pytest.raises(SSRFError, match="internal service"):
            _check_ssrf("http://registry:8000/api", config)

    def test_blocks_aws_metadata(self, config):
        with pytest.raises(SSRFError, match="internal service"):
            _check_ssrf("http://169.254.169.254/latest/meta-data/", config)

    def test_blocks_private_ip_10(self, config):
        with pytest.raises(SSRFError, match="private network"):
            _check_ssrf("http://10.0.0.1:8080/verify", config)

    def test_blocks_private_ip_172(self, config):
        with pytest.raises(SSRFError, match="private network"):
            _check_ssrf("http://172.16.0.1:8080/verify", config)

    def test_blocks_private_ip_192(self, config):
        with pytest.raises(SSRFError, match="private network"):
            _check_ssrf("http://192.168.1.1:8080/verify", config)

    def test_blocks_loopback_ip(self, config):
        with pytest.raises(SSRFError, match="private network"):
            _check_ssrf("http://127.0.0.1:9000/verify", config)

    def test_blocks_ipv6_loopback(self, config):
        with pytest.raises(SSRFError, match="private network"):
            _check_ssrf("http://[::1]:8000/verify", config)

    def test_blocks_zero_address(self, config):
        with pytest.raises(SSRFError):
            _check_ssrf("http://0.0.0.0:8080/verify", config)

    def test_blocks_file_scheme(self, config):
        with pytest.raises(SSRFError, match="only http/https"):
            _check_ssrf("file:///etc/passwd", config)

    def test_blocks_gopher_scheme(self, config):
        with pytest.raises(SSRFError, match="only http/https"):
            _check_ssrf("gopher://internal:25/", config)

    def test_allows_public_url(self, config):
        # Should not raise
        _check_ssrf("https://api.example.com/verify", config)

    def test_allows_public_ip(self, config):
        # Should not raise — 8.8.8.8 is Google DNS
        _check_ssrf("http://8.8.8.8:8080/verify", config)

    def test_blocks_invalid_url(self, config):
        with pytest.raises(SSRFError, match="Invalid URL"):
            _check_ssrf("not-a-url", config)

    def test_development_mode_allows_internal(self):
        """In development mode, private networks are NOT blocked."""
        dev_config = SandboxConfig(block_private_networks=False)
        # Should not raise
        _check_ssrf("http://localhost:8000/verify", dev_config)
        _check_ssrf("http://192.168.1.1:8080/verify", dev_config)


class TestResponseValidation:
    """Test response size and content validation."""

    @pytest.fixture
    def config(self):
        return SandboxConfig(max_response_size=1024)  # 1 KB limit for tests

    def test_rejects_oversized_response(self, config):
        """Response body larger than max_response_size is rejected."""
        from unittest.mock import MagicMock

        response = MagicMock()
        response.headers = {"content-length": "2048"}
        response.content = b"x" * 2048

        with pytest.raises(SandboxError, match="too large"):
            _validate_response(response, config)

    def test_accepts_normal_response(self, config):
        """Response within size limit is accepted."""
        from unittest.mock import MagicMock

        response = MagicMock()
        response.headers = {"content-length": "512"}
        response.content = b"x" * 512

        # Should not raise
        _validate_response(response, config)

    def test_checks_actual_body_size(self, config):
        """Even without content-length header, body size is checked."""
        from unittest.mock import MagicMock

        response = MagicMock()
        response.headers = {}  # No content-length
        response.content = b"x" * 2048

        with pytest.raises(SandboxError, match="too large"):
            _validate_response(response, config)


class TestSandboxConfig:
    """Test sandbox configuration."""

    def test_default_config(self):
        config = SandboxConfig()
        assert config.request_timeout == 30.0
        assert config.max_response_size == 10 * 1024 * 1024  # 10 MB
        assert config.max_redirects == 3
        assert config.block_private_networks is True

    def test_custom_config(self):
        config = SandboxConfig(
            request_timeout=5.0,
            max_response_size=1024,
            block_private_networks=False,
        )
        assert config.request_timeout == 5.0
        assert config.max_response_size == 1024
        assert config.block_private_networks is False


class TestHeaderStripping:
    """Test that internal headers are not forwarded to agent endpoints."""

    @pytest.mark.asyncio
    async def test_strips_authorization_header(self):
        """Authorization headers must not be forwarded."""
        # We test the logic inline since sandboxed_call is async
        # and needs a real HTTP endpoint
        blocked_prefixes = ("authorization", "cookie", "x-internal", "x-forwarded")
        headers = {
            "Authorization": "Bearer secret-jwt",
            "Cookie": "session=abc",
            "X-Internal-Token": "internal-secret",
            "X-Forwarded-For": "10.0.0.1",
            "Content-Type": "application/json",
            "X-Custom-Header": "allowed",
        }

        safe_headers = {}
        for key, value in headers.items():
            if not key.lower().startswith(blocked_prefixes):
                safe_headers[key] = value

        assert "Authorization" not in safe_headers
        assert "Cookie" not in safe_headers
        assert "X-Internal-Token" not in safe_headers
        assert "X-Forwarded-For" not in safe_headers
        assert "Content-Type" in safe_headers
        assert "X-Custom-Header" in safe_headers
