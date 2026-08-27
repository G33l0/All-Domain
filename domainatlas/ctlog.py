"""Certificate Transparency log reading (RFC 6962).

CT logs are a public append-only protocol served by the log operators
themselves. Aggregators such as crt.sh and certstream sit on top of these logs
and are the usual point of failure; reading the logs directly removes that
dependency and leaves several independent operators to fall back on.

Certificates are parsed with a minimal DER scan rather than a cryptography
dependency: only the subjectAltName extension is needed, and it has a fixed,
easily located structure.
"""

from __future__ import annotations

import base64
import struct
from dataclasses import dataclass
from typing import Dict, Iterable, List, Sequence

#: OID 2.5.29.17, subjectAltName.
SAN_OID = b"\x06\x03\x55\x1d\x11"

#: Public logs, one entry per operator so a single outage cannot stop
#: discovery. Shards roll over yearly; unreachable entries are skipped.
DEFAULT_CT_LOGS: Sequence[Dict[str, str]] = (
    {"name": "cloudflare-nimbus2026", "url": "https://ct.cloudflare.com/logs/nimbus2026/"},
    {"name": "digicert-wyvern2026h1", "url": "https://wyvern.ct.digicert.com/2026h1/"},
    {"name": "sectigo-sabre2026h1", "url": "https://sabre2026h1.ct.sectigo.com/"},
    {"name": "google-argon2026h1", "url": "https://ct.googleapis.com/logs/us/argon2026h1/"},
    {"name": "letsencrypt-oak2026h1", "url": "https://oak.ct.letsencrypt.org/2026h1/"},
    {"name": "cloudflare-nimbus2025", "url": "https://ct.cloudflare.com/logs/nimbus2025/"},
)


@dataclass
class LogState:
    """Where reading of one log has reached."""

    name: str
    url: str
    position: int = 0
    tree_size: int = 0
    failures: int = 0


def _read_length(data: bytes, index: int):
    """DER length at *index*, returned with the index that follows it."""
    if index >= len(data):
        return None, index
    first = data[index]
    index += 1
    if first < 0x80:
        return first, index
    count = first & 0x7F
    if count == 0 or count > 4 or index + count > len(data):
        return None, index
    return int.from_bytes(data[index:index + count], "big"), index + count


def dns_names(der: bytes) -> List[str]:
    """Every dNSName held in the certificate's subjectAltName extension."""
    names: List[str] = []
    position = 0
    while True:
        found = der.find(SAN_OID, position)
        if found < 0:
            break
        position = found + len(SAN_OID)
        cursor = position
        if cursor < len(der) and der[cursor] == 0x01:        # optional critical flag
            length, cursor = _read_length(der, cursor + 1)
            if length is None:
                continue
            cursor += length
        if cursor >= len(der) or der[cursor] != 0x04:        # OCTET STRING wrapper
            continue
        length, cursor = _read_length(der, cursor + 1)
        if length is None:
            continue
        end = min(len(der), cursor + length)
        if cursor >= len(der) or der[cursor] != 0x30:        # SEQUENCE OF GeneralName
            continue
        sequence_length, cursor = _read_length(der, cursor + 1)
        if sequence_length is None:
            continue
        end = min(end, cursor + sequence_length)
        while cursor < end:
            tag = der[cursor]
            length, cursor = _read_length(der, cursor + 1)
            if length is None:
                break
            if tag == 0x82:                                  # [2] dNSName
                names.append(der[cursor:cursor + length].decode("ascii", "ignore"))
            cursor += length
    return names


def leaf_certificate(leaf_input: bytes) -> bytes:
    """The certificate, or pre-certificate TBS, inside a MerkleTreeLeaf."""
    if len(leaf_input) < 12:
        return b""
    entry_type = struct.unpack(">H", leaf_input[10:12])[0]
    cursor = 12
    if entry_type == 1:                                      # precert: issuer key hash first
        cursor += 32
    if cursor + 3 > len(leaf_input):
        return b""
    length = int.from_bytes(leaf_input[cursor:cursor + 3], "big")
    cursor += 3
    return leaf_input[cursor:cursor + length]


def names_from_entry(entry: Dict[str, str]) -> List[str]:
    """DNS names from one ``get-entries`` item."""
    names: List[str] = []
    leaf = entry.get("leaf_input")
    if leaf:
        try:
            names.extend(dns_names(leaf_certificate(base64.b64decode(leaf))))
        except Exception:
            names = []
    if not names and entry.get("extra_data"):
        try:
            names.extend(dns_names(base64.b64decode(entry["extra_data"])))
        except Exception:
            pass
    return names


def names_from_entries(entries: Iterable[Dict[str, str]]) -> List[str]:
    names: List[str] = []
    for entry in entries:
        names.extend(names_from_entry(entry))
    return names
