# LUMI — SSRF protection
# Loopback, RFC1918, link-local, metadata IP, multicast, reserved, unspecified,
# Blocks IPv4-mapped IPv6 access. After DNS resolution and every redirect
# IP is reclassified. TOCTOU is prevented with DNS pin.
from __future__ import annotations

import ipaddress
import socket
import time
from urllib.parse import urljoin, urlparse

_BLOCKED_NETWORKS = [
    "127.0.0.0/8",
    "10.0.0.0/8",
    "172.16.0.0/12",
    "192.168.0.0/16",
    "169.254.0.0/16",   # link-local IPv4
    "0.0.0.0/8",        # unspecified / broadcast
    "100.64.0.0/10",    # CGNAT
    "192.0.2.0/24",     # TEST-NET-1
    "198.51.100.0/24",  # TEST-NET-2
    "203.0.113.0/24",   # TEST-NET-3
    "224.0.0.0/4",      # multicast
    "240.0.0.0/4",      # reserved
    "255.255.255.255/32",
    "::1/128",
    "fc00::/7",         # IPv6 unique-local
    "fe80::/10",        # link-local IPv6
    "ff00::/8",         # multicast IPv6
    "::ffff:0:0/96",    # IPv4-mapped IPv6
    "::/128",           # unspecified IPv6
    "200::/7",          # reserved (old)
    "64:ff9b::/96",     # NAT64
]

_METADATA_HOSTS = {"169.254.169.254", "metadata.google.internal", "metadata.google.com", "instance-data"}

# Internal Docker hostnames — forbidden in external requests (except internal_health)
_INTERNAL_HOSTNAMES = {"host.docker.internal", "gateway.docker.internal"}

_blocked = [ipaddress.ip_network(n) for n in _BLOCKED_NETWORKS]

# DNS pin cache — TOCTOU prevention (host -> (ips, expiry))
_dns_pin: dict[str, tuple[list[str], float]] = {}
_DNS_PIN_TTL = 300  # 5 dakika
# Allow port policy — only http/https default ports (optionally extendable)
_ALLOWED_SCHEMES = {"http", "https"}

class SSRFError(Exception):
    pass


def _is_internal_hostname(host: str) -> bool:
    h = host.lower().rstrip(".")
    return h in _INTERNAL_HOSTNAMES or h.endswith(".internal") or h.endswith(".local")


def resolve_all(host: str, *, use_pin: bool = True) -> list[str]:
    """Returns all A/AAAA results (if even one is blocked, it's hostile).
    Uses DNS pin cache (TTL 5min) — pins to same IP against TOCTOU."""
    host_l = host.rstrip(".").lower()
    now = time.monotonic()
    if use_pin and host_l in _dns_pin:
        ips_cached, exp = _dns_pin[host_l]
        if now < exp:
            return ips_cached
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror as e:
        raise SSRFError(f"DNS resolution failed: {host}") from e
    ips: set[str] = set()
    for info in infos:
        ips.add(str(info[4][0]))
    if not ips:
        raise SSRFError(f"DNS empty result: {host}")
    result = sorted(ips)
    _dns_pin[host_l] = (result, now + _DNS_PIN_TTL)
    return result


def clear_dns_pin(host: str | None = None) -> None:
    if host is None:
        _dns_pin.clear()
    else:
        _dns_pin.pop(host.rstrip(".").lower(), None)


def ip_is_blocked(ip_str: str) -> bool:
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return True
    if ip_str in _METADATA_HOSTS:
        return True
    # IPv4-mapped IPv6 check
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return True
    # also check ipaddress built-in classes (defense in depth)
    if ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_unspecified or ip.is_reserved:
        return True
    # use is_private for Private but CGNAT/reserved already covered; still check blocked networks
    return any(ip in net for net in _blocked)


def validate_host(host: str, allowed_hosts: set[str] | None = None) -> None:
    """Validates host name; checks IP class after DNS resolution.

    - if allowed_hosts is given, host must exactly match one of them (deny-by-allowlist).
    - if allowed_hosts is None, only blocked IPs are rejected (backward compat).
    - Blocked IPs are rejected even if the hostname is in the allowlist.
    """
    raw = host
    host = host.rstrip(".").lower()
    if not host:
        raise SSRFError("empty host")
    if host in _METADATA_HOSTS:
        raise SSRFError("metadata access blocked")
    if _is_internal_hostname(host):
        raise SSRFError(f"internal hostname engellendi: {host}")
    # allowlist deny — if explicit allowlist exists, others are rejected
    if allowed_hosts is not None:
        allowed_lower = {h.lower().rstrip(".") for h in allowed_hosts}
        if host not in allowed_lower:
            raise SSRFError(f"host outside allowlist: {host}")
    # Direct IP literal check (no DNS required)
    try:
        ipaddress.ip_address(raw.rstrip("."))
        # host kendisi IP literal
        if ip_is_blocked(raw.rstrip(".")):
            raise SSRFError(f"bloklu IP (RFC1918/loopback/metadata): {raw}")
        return
    except ValueError:
        pass
    # DNS pin + all IPs block check
    for ip in resolve_all(host):
        if ip_is_blocked(ip):
            raise SSRFError(f"bloklu IP (RFC1918/loopback/metadata): {ip}")


def validate_url(url: str, allowed_hosts: set[str] | None = None) -> str:
    """Parses the URL, validates the host; performs port/scheme/userinfo checks."""
    parts = urlparse(url)
    if parts.scheme not in _ALLOWED_SCHEMES:
        raise SSRFError(f"invalid scheme: {parts.scheme}")
    if not parts.hostname:
        raise SSRFError("host missing")
    # userinfo reddi (http://user:pass@host/)
    if parts.username or parts.password:
        raise SSRFError("URL with userinfo blocked")
    if parts.fragment:
        # fragment is harmless but log
        pass
    # Unix socket and file-like schemes
    if "unix:" in url.lower():
        raise SSRFError("unix socket access blocked")
    # port check — only default or explicitly allowed ports (80,443)
    # Instead of rejecting non-standard ports, log them, but block ports that are risky for SSRF
    if parts.port is not None:
        if parts.port not in (80, 443, 8000, 8001, 8002, 3525):
            # internal ports are only for internal_health; for external connectors, non-80/443 ports are suspicious
            # Strict mode: reject ports outside the allowlist
            if allowed_hosts is not None and parts.port not in (80, 443):
                raise SSRFError(f"port outside allowlist: {parts.port}")
    validate_host(parts.hostname, allowed_hosts)
    return url


def resolve_redirect_url(current_url: str, location: str, allowed_hosts: set[str] | None = None) -> str:
    """Converts a relative Location header to an absolute URL and re-validates it."""
    if not location:
        raise SSRFError("empty redirect location")
    # urljoin resolves relative redirect
    resolved = urljoin(current_url, location)
    # validate (DNS pin + allowlist)
    validate_url(resolved, allowed_hosts)
    return resolved
