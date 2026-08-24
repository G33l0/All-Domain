"""Domain parsing, validation and normalisation.

``normalize_domain`` turns anything that looks like a host reference
(``https://Example.COM:8443/path``, ``*.example.com.``, ``bücher.de``) into a
canonical, ASCII, lower-case registrable host name, or ``None`` when the input
is not a usable domain.  The result doubles as the deduplication fingerprint.
"""

from __future__ import annotations

import re
from typing import Iterable, Iterator, List, Optional

import idna

MAX_DOMAIN_LENGTH = 253
MAX_LABEL_LENGTH = 63

_IPV4_RE = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")
_LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_TLD_RE = re.compile(r"^(?:[a-z]{2,63}|xn--[a-z0-9-]{2,59})$")


def normalize_domain(raw: object) -> Optional[str]:
    """Return the canonical form of *raw*, or ``None`` if it is not a domain."""
    if not isinstance(raw, str):
        return None

    value = raw.strip()
    if not value:
        return None

    # Strip a URL scheme and everything after the authority component.
    if "://" in value:
        value = value.split("://", 1)[1]
    for separator in ("/", "?", "#", "\\"):
        value = value.split(separator, 1)[0]

    # user:password@host
    if "@" in value:
        value = value.rsplit("@", 1)[1]

    value = value.strip().lower()
    if not value:
        return None

    # Bracketed IPv6 literals are not domains.
    if value.startswith("["):
        return None

    # Strip the port.  A bare IPv6 address contains several colons - reject it.
    if value.count(":") > 1:
        return None
    if ":" in value:
        value = value.split(":", 1)[0]

    # Certificate transparency logs are full of wildcard entries.
    while value.startswith("*."):
        value = value[2:]
    value = value.lstrip(".").rstrip(".")

    if not value or "." not in value or ".." in value:
        return None
    if _IPV4_RE.match(value):
        return None
    if len(value) > MAX_DOMAIN_LENGTH:
        return None

    encoded = value
    if any(ord(char) > 127 for char in value):
        try:
            encoded = idna.encode(value, uts46=True, transitional=False).decode("ascii")
        except (idna.IDNAError, UnicodeError):
            return None

    labels = encoded.split(".")
    if len(labels) < 2:
        return None
    if any(len(label) > MAX_LABEL_LENGTH or not _LABEL_RE.match(label) for label in labels):
        return None
    if not _TLD_RE.match(labels[-1]):
        return None
    if len(encoded) > MAX_DOMAIN_LENGTH:
        return None
    return encoded


def is_valid_domain(raw: object) -> bool:
    """``True`` when *raw* normalises to a usable domain name."""
    return normalize_domain(raw) is not None


def registrable_suffix(domain: str) -> str:
    """Best-effort public suffix (last two labels, or three for ``co.uk`` style)."""
    labels = domain.split(".")
    if len(labels) >= 3 and len(labels[-2]) <= 3 and len(labels[-1]) <= 3:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def iter_normalized(values: Iterable[object]) -> Iterator[str]:
    """Yield unique normalised domains from an arbitrary iterable."""
    seen = set()
    for value in values:
        domain = normalize_domain(value)
        if domain and domain not in seen:
            seen.add(domain)
            yield domain


def normalize_all(values: Iterable[object]) -> List[str]:
    """Eager version of :func:`iter_normalized`."""
    return list(iter_normalized(values))


# Backwards compatible alias for the 1.x helper.
def domain_fingerprint(raw: object) -> Optional[str]:
    return normalize_domain(raw)
