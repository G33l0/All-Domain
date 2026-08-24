import re

from domaincollector.tech import categories_for, detect, detect_versions


def test_detects_from_headers_with_version():
    result = detect_versions({"Server": "nginx/1.24.0"}, "")
    assert result["Nginx"] == "1.24.0"
    assert "Nginx" in detect({"Server": "nginx/1.24.0"}, "")


def test_header_without_pattern_matches_on_presence():
    assert "Cloudflare" in detect({"CF-RAY": "abc123-LHR"}, "")
    assert "HSTS" in detect({"Strict-Transport-Security": "max-age=31536000"}, "")


def test_detects_from_cookies():
    headers = {"Set-Cookie": "PHPSESSID=deadbeef; path=/"}
    assert "PHP" in detect(headers, "")


def test_detects_from_meta_generator_and_implies():
    html = '<meta name="generator" content="WordPress 6.4.2" />'
    versions = detect_versions({}, html)
    assert versions["WordPress"] == "6.4.2"
    assert "PHP" in versions  # implied


def test_detects_reversed_meta_attribute_order():
    html = '<meta content="Drupal 10 (https://www.drupal.org)" name="generator">'
    assert "Drupal" in detect({}, html)


def test_detects_from_html_markup():
    html = '<script src="/static/jquery-3.6.0.min.js"></script><div data-reactroot>'
    names = detect({}, html)
    assert "jQuery" in names and "React" in names


def test_names_never_carry_version_numbers():
    html = '<script src="/static/jquery-3.6.0.min.js"></script>'
    headers = {"Server": "nginx/1.24.0", "X-Powered-By": "PHP/8.2"}
    names = detect(headers, html)
    assert names, "expected at least one detection"
    assert not any(re.search(r"\d+\.\d+", name) for name in names)
    assert detect_versions(headers, html)["jQuery"] == "3.6.0"


def test_empty_input_is_safe():
    assert detect() == []
    assert detect({}, "") == []
    assert detect(None, None) == []


def test_repeated_headers_are_joined():
    class MultiHeaders:
        def items(self):
            return [("Set-Cookie", "a=1"), ("Set-Cookie", "PHPSESSID=2")]

    assert "PHP" in detect(MultiHeaders(), "")


def test_categories():
    assert categories_for("Nginx") == "web-servers"
    assert categories_for("Totally Unknown") == "unknown"
