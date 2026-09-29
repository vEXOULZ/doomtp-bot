"""Which hosts and addresses `http get` may reach (ADR-0020 §Address rules).

Two separate questions. A *host* is allowed when a bot admin put it on the allow-list; an *address* is
refused when it is anywhere but the public internet, whatever name led to it. Both are asked on every
redirect hop, and the address check runs on the answers the connection is then made to, so a DNS answer
that changes between a check and a connect can't slip past it.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Iterable

# IPv6 forms that carry an IPv4 address inside: the embedded address is what gets reached.
_NAT64 = ipaddress.ip_network("64:ff9b::/96")
_NAT64_LOCAL = ipaddress.ip_network("64:ff9b:1::/48")
_TEREDO = ipaddress.ip_network("2001::/32")
_SIX_TO_FOUR = ipaddress.ip_network("2002::/16")


def refused(address: str) -> bool:
    """True unless `address` is a public unicast address. Anything that doesn't parse is refused."""
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])  # drop an IPv6 zone id
    except ValueError:
        return True
    if isinstance(ip, ipaddress.IPv6Address):
        if ip in _NAT64_LOCAL or ip in _TEREDO or ip in _SIX_TO_FOUR:
            return True  # tunnels whose far end we can't check
        embedded = ip.ipv4_mapped
        if embedded is None and ip in _NAT64:
            embedded = ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
        if embedded is not None:
            return refused(str(embedded))
    return (
        not ip.is_global  # private, loopback, link-local, CGNAT (100.64/10), documentation, ...
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_private
    )


def is_ip_literal(host: str) -> bool:
    """URLs name hosts; an address typed in a URL is refused outright, public or not."""
    try:
        ipaddress.ip_address(host.strip("[]").split("%", 1)[0])
    except ValueError:
        return False
    return True


def matching_pattern(host: str, patterns: Iterable[str]) -> str | None:
    """The allow-list entry that lets `host` through: an exact name, or `*.example.com` for any name one
    or more labels under example.com (but not example.com itself)."""
    host = host.lower().rstrip(".")
    for pattern in patterns:
        if pattern.startswith("*."):
            suffix = pattern[1:]  # ".example.com"
            if host.endswith(suffix) and len(host) > len(suffix):
                return pattern
        elif host == pattern:
            return pattern
    return None
