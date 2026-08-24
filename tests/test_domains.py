import pytest

from domaincollector.domains import (
    is_valid_domain,
    normalize_all,
    normalize_domain,
    registrable_suffix,
)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("example.com", "example.com"),
        ("EXAMPLE.COM", "example.com"),
        ("https://Example.COM/path?x=1", "example.com"),
        ("http://example.com:8080", "example.com"),
        ("example.com.", "example.com"),
        ("*.example.com", "example.com"),
        ("*.*.example.com", "example.com"),
        ("user:pass@example.com", "example.com"),
        ("  example.com  ", "example.com"),
        ("sub.domain.example.co.uk", "sub.domain.example.co.uk"),
        ("xn--bcher-kva.de", "xn--bcher-kva.de"),
        ("bücher.de", "xn--bcher-kva.de"),
        ("тест.рф", "xn--e1aybc.xn--p1ai"),
    ],
)
def test_normalises_valid_domains(raw, expected):
    assert normalize_domain(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "", "   ", None, 42, b"example.com",
        "localhost", "example", "a.b", "example.c",
        "192.168.0.1", "8.8.8.8", "[::1]", "::1",
        "-bad.com", "bad-.com", "foo_bar.com", "ex..ample.com",
        "http://", "///", "a" * 250 + ".com", "a" * 64 + ".com",
    ],
)
def test_rejects_non_domains(raw):
    assert normalize_domain(raw) is None
    assert is_valid_domain(raw) is False


def test_normalize_all_deduplicates_and_keeps_order():
    values = ["https://A.com", "a.com", "*.b.com", "junk", "b.com"]
    assert normalize_all(values) == ["a.com", "b.com"]


def test_registrable_suffix():
    assert registrable_suffix("www.example.com") == "example.com"
    assert registrable_suffix("shop.example.co.uk") == "example.co.uk"
