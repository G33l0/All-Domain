"""Technology fingerprinting.

A dependency-free rule engine over the response headers, cookies, meta tags and
HTML already fetched by the collector, so detection costs no extra requests.

The legacy ``builtwith`` package can be used alongside it when installed (see
``Config.use_builtwith``); it runs in a worker thread with the headers and HTML
supplied so it never performs its own blocking request.
"""

from __future__ import annotations

import importlib
import re
from dataclasses import dataclass
from typing import Dict, Iterable, List, Mapping, Optional, Pattern, Tuple

__all__ = [
    "Rule", "RULES", "detect", "detect_versions", "categories_for",
    "builtwith_available", "detect_with_builtwith",
]

_FLAGS = re.IGNORECASE


def _p(pattern: str) -> Pattern[str]:
    return re.compile(pattern, _FLAGS)


@dataclass(frozen=True)
class Rule:
    """A single technology fingerprint.

    Any pattern may expose a ``ver`` named group; when it matches, the version
    is appended to the technology name (``"WordPress 6.4"``).
    """

    name: str
    category: str
    headers: Tuple[Tuple[str, Optional[Pattern[str]]], ...] = ()
    cookies: Tuple[Pattern[str], ...] = ()
    html: Tuple[Pattern[str], ...] = ()
    meta: Tuple[Tuple[str, Pattern[str]], ...] = ()
    implies: Tuple[str, ...] = ()

    def match(self, headers: Mapping[str, str], cookies: str, html: str, metas: Mapping[str, str]) -> Optional[str]:
        """Return the detected version string (possibly ``""``) or ``None``."""
        for header_name, pattern in self.headers:
            value = headers.get(header_name.lower())
            if value is None:
                continue
            if pattern is None:
                return ""
            found = pattern.search(value)
            if found:
                return _version(found)
        for pattern in self.cookies:
            found = pattern.search(cookies)
            if found:
                return _version(found)
        for name, pattern in self.meta:
            value = metas.get(name.lower())
            if value is not None:
                found = pattern.search(value)
                if found:
                    return _version(found)
        if html:
            for pattern in self.html:
                found = pattern.search(html)
                if found:
                    return _version(found)
        return None


def _version(match: "re.Match[str]") -> str:
    """Extract the optional ``ver`` capture group from a rule match."""
    version = match.groupdict().get("ver")
    return (version or "").strip()


# --------------------------------------------------------------------- rules
RULES: Tuple[Rule, ...] = (
    # ---- web servers
    Rule("Nginx", "web-servers", headers=(("server", _p(r"nginx(?:/(?P<ver>[\d.]+))?")),)),
    Rule("Apache", "web-servers", headers=(("server", _p(r"apache(?:/(?P<ver>[\d.]+))?")),)),
    Rule("Microsoft IIS", "web-servers", headers=(("server", _p(r"microsoft-iis(?:/(?P<ver>[\d.]+))?")),)),
    Rule("LiteSpeed", "web-servers", headers=(("server", _p(r"litespeed")),)),
    Rule("OpenResty", "web-servers", headers=(("server", _p(r"openresty(?:/(?P<ver>[\d.]+))?")),)),
    Rule("Caddy", "web-servers", headers=(("server", _p(r"caddy")),)),
    Rule("Envoy", "web-servers", headers=(("server", _p(r"envoy")),)),
    Rule("Gunicorn", "web-servers", headers=(("server", _p(r"gunicorn(?:/(?P<ver>[\d.]+))?")),)),
    Rule("Werkzeug", "web-servers", headers=(("server", _p(r"werkzeug(?:/(?P<ver>[\d.]+))?")),)),
    Rule("Tomcat", "web-servers", headers=(("server", _p(r"tomcat(?:/(?P<ver>[\d.]+))?")),)),
    Rule("Jetty", "web-servers", headers=(("server", _p(r"jetty")),)),
    Rule("Kestrel", "web-servers", headers=(("server", _p(r"kestrel")),)),
    # ---- CDN / hosting
    Rule("Cloudflare", "cdn", headers=(("server", _p(r"cloudflare")), ("cf-ray", None))),
    Rule("Amazon CloudFront", "cdn", headers=(("via", _p(r"cloudfront")), ("x-amz-cf-id", None))),
    Rule("Fastly", "cdn", headers=(("x-served-by", _p(r"cache-")), ("x-fastly-request-id", None))),
    Rule("Akamai", "cdn", headers=(("server", _p(r"akamai")), ("x-akamai-transformed", None))),
    Rule("Vercel", "paas", headers=(("server", _p(r"vercel")), ("x-vercel-id", None))),
    Rule("Netlify", "paas", headers=(("server", _p(r"netlify")), ("x-nf-request-id", None))),
    Rule("GitHub Pages", "paas", headers=(("server", _p(r"github\.com")), ("x-github-request-id", None))),
    Rule("Amazon S3", "paas", headers=(("server", _p(r"amazons3")),)),
    Rule("Google Cloud", "paas", headers=(("server", _p(r"^gse$|google frontend")),)),
    Rule("Heroku", "paas", headers=(("via", _p(r"vegur")), ("server", _p(r"^cowboy$")))),
    Rule("Sucuri", "security", headers=(("x-sucuri-id", None),)),
    Rule("Varnish", "caching", headers=(("via", _p(r"varnish")), ("x-varnish", None))),
    # ---- languages / runtimes
    Rule("PHP", "programming-languages", headers=(("x-powered-by", _p(r"php(?:/(?P<ver>[\d.]+))?")),),
         cookies=(_p(r"PHPSESSID"),)),
    Rule("ASP.NET", "web-frameworks", headers=(("x-aspnet-version", _p(r"(?P<ver>[\d.]+)")),
                                               ("x-powered-by", _p(r"asp\.net"))),
         cookies=(_p(r"ASP\.NET_SessionId"),)),
    Rule("Java", "programming-languages", cookies=(_p(r"JSESSIONID"),)),
    Rule("Ruby on Rails", "web-frameworks", headers=(("x-powered-by", _p(r"phusion passenger")),),
         cookies=(_p(r"_rails_session|_session_id"),)),
    Rule("Django", "web-frameworks", cookies=(_p(r"csrftoken|django_language"),)),
    Rule("Express", "web-frameworks", headers=(("x-powered-by", _p(r"express")),)),
    Rule("Laravel", "web-frameworks", cookies=(_p(r"laravel_session|XSRF-TOKEN"),)),
    Rule("Flask", "web-frameworks", cookies=(_p(r"session=\.ey"),)),
    # ---- CMS / ecommerce
    Rule("WordPress", "cms",
         meta=(("generator", _p(r"wordpress(?:\s*(?P<ver>[\d.]+))?")),),
         html=(_p(r"/wp-(?:content|includes)/"),),
         implies=("PHP",)),
    Rule("Drupal", "cms",
         meta=(("generator", _p(r"drupal(?:\s*(?P<ver>[\d.]+))?")),),
         headers=(("x-generator", _p(r"drupal")), ("x-drupal-cache", None)),
         html=(_p(r"/sites/(?:all|default)/(?:themes|modules)/"),),
         implies=("PHP",)),
    Rule("Joomla", "cms",
         meta=(("generator", _p(r"joomla!?(?:\s*(?P<ver>[\d.]+))?")),),
         html=(_p(r"/media/(?:jui|system)/js/"),),
         implies=("PHP",)),
    Rule("Ghost", "cms", meta=(("generator", _p(r"ghost(?:\s*(?P<ver>[\d.]+))?")),)),
    Rule("Wix", "cms", headers=(("x-wix-request-id", None),), html=(_p(r"static\.wixstatic\.com"),)),
    Rule("Squarespace", "cms", html=(_p(r"static\d?\.squarespace\.com"),)),
    Rule("Shopify", "ecommerce", headers=(("x-shopify-stage", None),), html=(_p(r"cdn\.shopify\.com"),)),
    Rule("WooCommerce", "ecommerce", html=(_p(r"/plugins/woocommerce/"),), implies=("WordPress",)),
    Rule("Magento", "ecommerce", cookies=(_p(r"X-Magento-Vary"),), html=(_p(r"/(?:skin|static)/frontend/"),)),
    Rule("PrestaShop", "ecommerce", cookies=(_p(r"PrestaShop-"),)),
    Rule("HubSpot", "marketing", html=(_p(r"js\.hs-scripts\.com|hs-analytics\.net"),)),
    # ---- javascript
    Rule("jQuery", "javascript-frameworks", html=(_p(r"jquery[.-](?P<ver>\d+(?:\.\d+)+)(?:\.min)?\.js|/jquery(?:\.min)?\.js"),)),
    Rule("React", "javascript-frameworks", html=(_p(r"data-reactroot|__NEXT_DATA__|react(?:-dom)?(?:\.production)?(?:\.min)?\.js"),)),
    Rule("Next.js", "web-frameworks", headers=(("x-powered-by", _p(r"next\.js(?:\s*(?P<ver>[\d.]+))?")),),
         html=(_p(r"/_next/static/"),), implies=("React",)),
    Rule("Vue.js", "javascript-frameworks", html=(_p(r"data-v-[0-9a-f]{8}|vue(?:\.runtime)?(?:\.min)?\.js"),)),
    Rule("Nuxt.js", "web-frameworks", html=(_p(r"/_nuxt/|__NUXT__"),), implies=("Vue.js",)),
    Rule("Angular", "javascript-frameworks", html=(_p(r"ng-version=\"(?P<ver>[\d.]+)\"|angular(?:\.min)?\.js"),)),
    Rule("Svelte", "javascript-frameworks", html=(_p(r"svelte-[0-9a-z]{6}|/_app/immutable/"),)),
    Rule("Alpine.js", "javascript-frameworks", html=(_p(r"x-data=|alpinejs"),)),
    Rule("Bootstrap", "ui-frameworks", html=(_p(r"bootstrap(?:[.-](?P<ver>\d+(?:\.\d+)+))?(?:\.min)?\.(?:css|js)"),)),
    Rule("Tailwind CSS", "ui-frameworks", html=(_p(r"tailwind(?:css)?(?:\.min)?\.css|cdn\.tailwindcss\.com"),)),
    Rule("Font Awesome", "font-scripts", html=(_p(r"font-?awesome"),)),
    Rule("Google Font API", "font-scripts", html=(_p(r"fonts\.(?:googleapis|gstatic)\.com"),)),
    Rule("Modernizr", "javascript-libraries", html=(_p(r"modernizr(?:[.-](?P<ver>\d+(?:\.\d+)+))?(?:\.min)?\.js"),)),
    Rule("Lodash", "javascript-libraries", html=(_p(r"lodash(?:\.min)?\.js"),)),
    Rule("HTMX", "javascript-libraries", html=(_p(r"htmx(?:\.org)?(?:\.min)?\.js|hx-get="),)),
    # ---- analytics / tags
    Rule("Google Analytics", "analytics", html=(_p(r"google-analytics\.com/(?:ga|analytics)\.js|gtag\('config'|googletagmanager\.com/gtag"),)),
    Rule("Google Tag Manager", "tag-managers", html=(_p(r"googletagmanager\.com/gtm\.js|GTM-[A-Z0-9]{4,}"),)),
    Rule("Facebook Pixel", "analytics", html=(_p(r"connect\.facebook\.net/[^\"']+/fbevents\.js"),)),
    Rule("Matomo", "analytics", html=(_p(r"matomo\.js|piwik\.js"),)),
    Rule("Plausible", "analytics", html=(_p(r"plausible\.io/js/"),)),
    Rule("Hotjar", "analytics", html=(_p(r"static\.hotjar\.com"),)),
    Rule("Cloudflare Web Analytics", "analytics", html=(_p(r"static\.cloudflareinsights\.com"),)),
    # ---- misc
    Rule("reCAPTCHA", "security", html=(_p(r"google\.com/recaptcha|recaptcha/api\.js"),)),
    Rule("hCaptcha", "security", html=(_p(r"hcaptcha\.com/1/api\.js"),)),
    Rule("Open Graph", "miscellaneous", html=(_p(r"<meta[^>]+property=[\"']og:"),)),
    Rule("HSTS", "security", headers=(("strict-transport-security", None),)),
    Rule("Content Security Policy", "security", headers=(("content-security-policy", None),)),
    Rule("YouTube", "video-players", html=(_p(r"youtube(?:-nocookie)?\.com/embed/"),)),
    Rule("Vimeo", "video-players", html=(_p(r"player\.vimeo\.com/video/"),)),
)

_RULES_BY_NAME: Dict[str, Rule] = {rule.name: rule for rule in RULES}

_META_RE = re.compile(
    r"<meta\b[^>]*?\bname\s*=\s*[\"']([^\"']+)[\"'][^>]*?\bcontent\s*=\s*[\"']([^\"']*)[\"']",
    re.IGNORECASE,
)
_META_REVERSED_RE = re.compile(
    r"<meta\b[^>]*?\bcontent\s*=\s*[\"']([^\"']*)[\"'][^>]*?\bname\s*=\s*[\"']([^\"']+)[\"']",
    re.IGNORECASE,
)


def _parse_metas(html: str) -> Dict[str, str]:
    metas: Dict[str, str] = {}
    for name, content in _META_RE.findall(html):
        metas.setdefault(name.strip().lower(), content)
    for content, name in _META_REVERSED_RE.findall(html):
        metas.setdefault(name.strip().lower(), content)
    return metas


def _normalise_headers(headers: Mapping[str, str]) -> Tuple[Dict[str, str], str]:
    """Lower-case header names, joining repeated values (notably Set-Cookie)."""
    flat: Dict[str, str] = {}
    items: Iterable[Tuple[str, str]]
    if hasattr(headers, "items"):
        items = headers.items()  # works for dict and aiohttp CIMultiDict
    else:  # pragma: no cover - defensive
        items = []
    for key, value in items:
        key = str(key).lower()
        value = "" if value is None else str(value)
        if key in flat:
            flat[key] = f"{flat[key]}; {value}"
        else:
            flat[key] = value
    return flat, flat.get("set-cookie", "")


def detect_versions(
    headers: Optional[Mapping[str, str]] = None, html: str = "", url: str = ""
) -> Dict[str, str]:
    """Map every detected technology to its version (``""`` when unknown).

    Names are kept free of version numbers so that counters and the
    ``output/<technology>.txt`` files never fragment into ``jQuery 1.9.1``,
    ``jQuery 3.6.0``, ...
    """
    flat_headers, cookies = _normalise_headers(headers or {})
    html = html or ""
    metas = _parse_metas(html) if html else {}

    found: Dict[str, str] = {}
    for rule in RULES:
        version = rule.match(flat_headers, cookies, html, metas)
        if version is None:
            continue
        found[rule.name] = version
        for implied in rule.implies:
            found.setdefault(implied, "")
    return dict(sorted(found.items()))


def detect(headers: Optional[Mapping[str, str]] = None, html: str = "", url: str = "") -> List[str]:
    """Sorted names of the technologies detected in a single response."""
    return sorted(detect_versions(headers, html, url))


def categories_for(technology: str) -> str:
    """Category of a detected technology (``"unknown"`` when not a built-in rule)."""
    rule = _RULES_BY_NAME.get(technology)
    return rule.category if rule else "unknown"


# ------------------------------------------------------------------ builtwith
def builtwith_available() -> bool:
    try:  # pragma: no cover - depends on the environment
        importlib.import_module("builtwith")
    except Exception:
        return False
    return True


def detect_with_builtwith(url: str, headers: Mapping[str, str], html: str) -> List[str]:
    """Run the optional ``builtwith`` package without letting it hit the network.

    Both ``headers`` and ``html`` are supplied, which is what stops builtwith
    from performing its own blocking urllib request.  Any failure is swallowed
    and reported as "no technologies".
    """
    try:  # pragma: no cover - optional dependency
        import builtwith as _builtwith

        flat, _ = _normalise_headers(headers or {})
        detected = _builtwith.builtwith(url, headers=flat, html=html or "")
        names: List[str] = []
        for items in (detected or {}).values():
            names.extend(str(item) for item in items)
        return sorted(set(names))
    except Exception:
        return []
