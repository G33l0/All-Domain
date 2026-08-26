"""Certificate parsing for the direct Certificate Transparency reader."""

import base64
import struct

from domainatlas.ctlog import (
    DEFAULT_CT_LOGS,
    dns_names,
    leaf_certificate,
    names_from_entries,
    names_from_entry,
)


def _san_extension(names):
    """Build a DER subjectAltName extension holding *names*."""
    general_names = b"".join(
        bytes([0x82, len(name)]) + name.encode("ascii") for name in names
    )
    sequence = bytes([0x30, len(general_names)]) + general_names
    octet_string = bytes([0x04, len(sequence)]) + sequence
    return b"\x06\x03\x55\x1d\x11" + octet_string


def _leaf(entry_type, payload):
    header = b"\x00\x00" + struct.pack(">Q", 0) + struct.pack(">H", entry_type)
    if entry_type == 1:
        header += b"\x00" * 32
    return header + len(payload).to_bytes(3, "big") + payload


def test_reads_dns_names_from_a_san_extension():
    der = b"\x30\x82\x01\x00" + _san_extension(["example.com", "www.example.com"])
    assert dns_names(der) == ["example.com", "www.example.com"]


def test_handles_a_critical_flag_before_the_value():
    names = ["a.example"]
    general = b"".join(bytes([0x82, len(n)]) + n.encode() for n in names)
    sequence = bytes([0x30, len(general)]) + general
    der = (b"\x06\x03\x55\x1d\x11" + b"\x01\x01\xff"
           + bytes([0x04, len(sequence)]) + sequence)
    assert dns_names(der) == ["a.example"]


def test_ignores_non_dns_general_names():
    email_value = b"x@example.io"
    dns_value = b"good.example"
    email = bytes([0x81, len(email_value)]) + email_value        # [1] rfc822Name
    dns = bytes([0x82, len(dns_value)]) + dns_value              # [2] dNSName
    general = email + dns
    sequence = bytes([0x30, len(general)]) + general
    der = b"\x06\x03\x55\x1d\x11" + bytes([0x04, len(sequence)]) + sequence
    assert dns_names(der) == ["good.example"]


def test_malformed_input_never_raises():
    for payload in (b"", b"\x06\x03\x55\x1d\x11", b"\x06\x03\x55\x1d\x11\x04",
                    b"\x06\x03\x55\x1d\x11\x04\xff\x30", b"\x00" * 64,
                    b"\x06\x03\x55\x1d\x11\x04\x84\xff\xff\xff\xff"):
        assert dns_names(payload) == []


def test_reads_a_certificate_out_of_a_merkle_leaf():
    certificate = b"\x30\x03" + _san_extension(["leaf.example"])
    assert leaf_certificate(_leaf(0, certificate)) == certificate


def test_reads_a_precertificate_out_of_a_merkle_leaf():
    tbs = b"\x30\x03" + _san_extension(["pre.example"])
    assert leaf_certificate(_leaf(1, tbs)) == tbs


def test_short_leaf_is_ignored():
    assert leaf_certificate(b"\x00\x00") == b""


def test_names_from_entry_uses_the_leaf():
    certificate = _san_extension(["entry.example"])
    entry = {"leaf_input": base64.b64encode(_leaf(0, certificate)).decode(), "extra_data": ""}
    assert names_from_entry(entry) == ["entry.example"]


def test_names_from_entry_falls_back_to_extra_data():
    """Pre-certificate chains carry the names in extra_data."""
    entry = {
        "leaf_input": base64.b64encode(_leaf(0, b"\x30\x00")).decode(),
        "extra_data": base64.b64encode(_san_extension(["extra.example"])).decode(),
    }
    assert names_from_entry(entry) == ["extra.example"]


def test_names_from_entry_tolerates_rubbish():
    assert names_from_entry({"leaf_input": "not base64!!", "extra_data": ""}) == []
    assert names_from_entry({}) == []


def test_names_from_entries_collects_across_entries():
    entries = [
        {"leaf_input": base64.b64encode(_leaf(0, _san_extension([f"h{i}.example"]))).decode(),
         "extra_data": ""}
        for i in range(3)
    ]
    assert names_from_entries(entries) == ["h0.example", "h1.example", "h2.example"]


def test_the_bundled_log_list_covers_several_operators():
    operators = {entry["url"].split("//")[1].split("/")[0].split(".")[-2] for entry in DEFAULT_CT_LOGS}
    assert len(operators) >= 3
    assert all(entry["url"].endswith("/") for entry in DEFAULT_CT_LOGS)
