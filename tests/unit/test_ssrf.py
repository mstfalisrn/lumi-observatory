# LUMI — SSRF birim testleri
import pytest

from connectors.ssrf import SSRFError, validate_host, validate_url


class TestSSRF:
    @pytest.mark.parametrize("host", [
        "127.0.0.1", "127.0.0.2", "localhost", "10.0.0.1", "172.20.0.2",
        "192.168.1.5", "169.254.169.254", "0.0.0.0",
    ])
    def test_blocked_hosts(self, host):
        with pytest.raises(SSRFError):
            validate_host(host)

    def test_public_host_allowed_when_allowedlist_empty(self):
        # public host open to the internet (if DNS resolves) — not loopback
        validate_host("8.8.8.8")

    def test_allowlist_restricts(self):
        with pytest.raises(SSRFError):
            validate_host("example.com", allowed_hosts={"github.com"})

    def test_unix_socket_blocked(self):
        with pytest.raises(SSRFError):
            validate_url("http://localhost:9999/trigger?x=unix:/tmp/x")

    def test_bad_scheme(self):
        with pytest.raises(SSRFError):
            validate_url("file:///etc/passwd")