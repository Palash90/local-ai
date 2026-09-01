"""URL classification shared by the web-search tool and the story pipeline.

The ``web_search`` tool scrubs its results before they enter the LLM context
(and before they are recorded as ``_search_details``), and ``self-chat.py``
uses the same rules to keep ad/promo and landing-page URLs out of citations
and the published story. Keeping the classifier here with no server-side
dependencies gives both sides one consistent definition.

Blocking is primarily DOMAIN-based, because path shapes overlap heavily
between retail listings and news CMS URLs (``/detail/``, ``/item/``,
``/p/`` appear in both). Three precise domain layers:

1. pure retail/marketplace domains — every page is a product/listing page;
2. ad/tracker network domains (doubleclick, taboola, outbrain, criteo…) —
   sponsored/redirect links that do show up in search results but are never
   content;
3. shopping host patterns — ``shop.``/``store.`` subdomains and the
   ``.shop``/``.store``/``.boutique`` TLDs.

Path segments are only a secondary signal: a small set of *unambiguous*
commerce/auth segments (``cart``, ``checkout``, ``sku``…) block outright,
while ambiguous CMS segments (``detail``, ``item``, ``home``…) merely mark a
page as a possible landing/listing page (dropped only when a real deep link
exists in the same result set). A hyphenated article slug in the final path
segment (``dark-patterns-explained``, ``story-123456``) protects a URL from
the ambiguous-segment rules — that shape is how real articles end.
"""

import re
from urllib.parse import urlparse

_LANDING_SEGMENT_RE = re.compile(
    r"^(section|sections|category|categories|topics?|tags?|archive|index)$"
)

# Locale prefixes ("/en-in/", "/en-us/news", "/de-de/") are regional homepages
# or section fronts, never articles. They must be classified BEFORE the
# hyphenated-slug rule, which otherwise matches them ("en-in" is indistinguish-
# able from a short slug) and lets a regional root masquerade as a deep link —
# evicting genuine landing pages from a landing-only result set.
_LOCALE_SEGMENT_RE = re.compile(r"[a-z]{2}(?:-[a-z]{2,4})?", re.I)

# URL path segments that mark a site-internal search/category/listing page —
# never an individual article ("flipkart.com/q/fashion-tops",
# "nykaafashion.com/women/tops/c/4497", "meesho.com/tops-ladies/pl/3ja").
# Includes the AMBIGUOUS CMS segments (detail, item, home, download…) that
# also appear inside real article paths — there they only demote the URL to
# landing class (dropped when a deep link exists), never blocked outright,
# and a slug-like final segment overrides them entirely.
_STRUCTURAL_PATH_SEGMENTS = {
    "q", "s", "c", "pl", "pr", "p", "products", "product", "category",
    "categories", "collection", "collections", "shop", "store", "search",
    "browse", "listings", "listing", "tagged", "detail", "details", "item",
    "items", "home", "catalog", "catalogue", "download", "downloads",
    "driver", "drivers", "apps", "price", "prices", "pricing", "portfolio",
    "shopping", "discount", "promo", "promotions", "dictionary", "topics",
}

# URL path segments that unambiguously mark a commerce/auth action page —
# a shopping cart, checkout, wishlist, coupon wall, app store SKU page, a
# login/signup form, a job board. No news CMS puts an article under these,
# so they block outright regardless of what the final slug looks like.
_AD_PATH_SEGMENTS = {
    "account", "bag", "basket", "career", "careers", "cart", "checkout",
    "coupon", "coupons", "deal", "deals", "driver", "drivers", "jobs",
    "login", "offer", "offers", "order", "orders", "pdp", "plp",
    "product", "products", "register", "sale", "seller", "signin",
    "signup", "sku", "vendor", "wishlist",
}

# Domain suffix list for domains that are pure retail/marketplace/classified
# sites — every page there is a product or listing page, never an article.
_AD_DOMAIN_SUFFIXES = (
    "zara.com", "zara.co.in", "andindia.com", "myntra.com", "flipkart.com",
    "amazon.com", "amazon.in", "amazon.co.uk", "amazon.de", "ajio.com",
    "meesho.com", "nykaa.com", "nykaafashion.com", "snapdeal.com",
    "shopclues.com", "ebay.com", "ebay.in", "ebay.co.uk", "etsy.com",
    "alibaba.com", "aliexpress.com", "walmart.com", "target.com",
    "bestbuy.com", "homedepot.com", "lowes.com", "wayfair.com", "newegg.com",
    "overstock.com", "asos.com", "shein.com", "temu.com", "wish.com",
    "croma.com", "tatacliq.com", "reliancedigital.in", "vijaysales.com",
    "jabong.com", "koovs.com", "limeroad.com", "firstcry.com",
    "paytmmall.com", "indiamart.com", "tradeindia.com", "olx.in",
    "quikr.com", "ikea.com", "nike.com", "adidas.com", "rakuten.com",
)

# Ad/tracker network domains: sponsored-content and redirect/track-pixel
# hosts that surface in search results (Outbrain/Taboola widgets especially)
# but never carry citable content. Blocked outright.
_AD_NETWORK_HOSTS = (
    "doubleclick.net", "googlesyndication.com", "googleadservices.com",
    "adservice.google.com", "adnxs.com", "adsrvr.org", "criteo.com",
    "criteo.net", "taboola.com", "outbrain.com", "pubmatic.com",
    "rubiconproject.com", "openx.net", "smartadserver.com", "media.net",
    "mgid.com", "revcontent.com", "scorecardresearch.com", "quantserve.com",
    "casalemedia.com", "33across.com", "moatads.com", "zedo.com",
    "bidswitch.net", "sharethrough.com", "yieldmo.com", "teads.tv",
    "amazon-adsystem.com",
)

# Subdomain labels and TLDs that mark a shopping property
# ("shop.outlet.com", "store.brand.com", "mystore.shop").
_SHOP_HOST_LABELS = ("shop", "store", "shopping", "boutique", "market")
_SHOP_TLDS = ("shop", "store", "boutique")

# URL patterns that mark product-list/product-detail or reference pages:
# an Amazon/Walmart product "dp/item" page, a dictionary entry, a stock
# symbol lookup, or a company-profile page. NOTE: bare numeric article IDs
# and date-prefixed slugs (/2026/09/01/...) must NOT be treated as ad links —
# those are the shape of a real article URL.
_AD_URL_RE = re.compile(
    r"(?:/dp/|/itm/|/dictionary/|/\d+-\d+-\d+-/|\bsku\s*=\s*[A-Z0-9-]+)", re.I
)
_AD_PROFILE_HOSTS = ("linkedin.com",)

# A hyphenated multi-part final path segment — "dark-patterns-explained",
# "story-123456", "technology-67123456" — is how real article slugs end.
# Bare numbers ("4497"), short IDs ("3ja") and single words ("technology")
# do not match, so listing/section pages are unaffected.
_ARTICLE_SLUG_RE = re.compile(r"[a-z]{2,}(?:-[a-z0-9]+)+", re.I)


def _host_matches(host, suffixes):
    """True if ``host`` equals or is a subdomain of any entry in ``suffixes``."""
    return any(host == s or host.endswith("." + s) for s in suffixes)


def _final_slug(url):
    """Return the final URL path segment (extension stripped, lowercased),
    or None when the path has no segments."""
    try:
        path = urlparse(url).path or "/"
    except (ValueError, AttributeError):
        return None
    segs = [s for s in path.split("/") if s]
    if not segs:
        return None
    return segs[-1].split(".")[0].lower()


def _ad_reason(url):
    """Return a human-readable reason string if ``url`` is an ad URL, else None.

    Same rules as :func:`is_ad_url` (which is a thin boolean wrapper) — kept
    separate so the scrubber can log *which* rule killed a link.
    """
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        path = parsed.path or "/"
    except (ValueError, AttributeError):
        return None
    if not host:
        return None
    if _host_matches(host, _AD_DOMAIN_SUFFIXES):
        return f"retail domain {host}"
    if _host_matches(host, _AD_NETWORK_HOSTS):
        return f"ad network {host}"
    first_label = host.split(".")[0]
    if first_label in _SHOP_HOST_LABELS:
        return f"shop host '{first_label}.'"
    if host.rsplit(".", 1)[-1] in _SHOP_TLDS:
        return f"shopping TLD '.{host.rsplit('.', 1)[-1]}'"
    for s in _AD_PROFILE_HOSTS:
        if host == s or host.endswith("." + s):
            if any(seg.startswith("company") for seg in path.split("/") if seg):
                return "profile host company page"
            return None
    m = _AD_URL_RE.search(url)
    if m:
        return f"url pattern {m.group(0)!r}"
    for seg in path.split("/"):
        seg = seg.split(".")[0].lower()
        if seg in _AD_PATH_SEGMENTS:
            return f"commerce segment '{seg}'"
    return None


def is_ad_url(url):
    """True for promotional shopping/ad-network URLs.

    News citations must be article pages; a product listing, cart/checkout
    page, driver download, sponsored-content redirect, or company profile
    never is. Domain-based rules (retail domains, ad networks, shop hosts)
    carry the load; path segments only back them up where unambiguous.
    """
    return _ad_reason(url) is not None


def _landing_reason(url):
    """Return a human-readable reason string if ``url`` is a landing page, else None.

    Same rules as :func:`is_landing_url` (which is a thin boolean wrapper) —
    kept separate so the scrubber can log *which* rule classified the link.
    """
    try:
        parsed = urlparse(url)
        path = parsed.path or "/"
    except (ValueError, AttributeError):
        return None
    path = path.rstrip("/")
    if not path:
        return "site root"
    segs = [s for s in path.split("/") if s]
    if not segs:
        return "site root"
    ad = _ad_reason(url)
    if ad:
        return f"ad URL ({ad})"
    first = segs[0].split(".")[0].lower()
    if _LANDING_SEGMENT_RE.match(first):
        return f"landing segment '{first}'"
    if len(segs) <= 2 and _LOCALE_SEGMENT_RE.fullmatch(first):
        return f"locale homepage '{first}'"
    last = segs[-1].split(".")[0].lower()
    if _ARTICLE_SLUG_RE.search(last):
        # A hyphenated article slug in the final segment wins over the
        # ambiguous structural segments in the middle of the path
        # ("/article/technology/detail/slug-123" is an article).
        return None
    for seg in segs:
        low = seg.split(".")[0].lower()
        if low in _STRUCTURAL_PATH_SEGMENTS:
            return f"structural segment '{low}'"
    if len(segs) == 1:
        seg = segs[0].lower()
        # One clean segment with no file extension and no digits ("/technology")
        # is a section page; real article slugs almost always carry a date, an
        # ID, or a hyphenated multi-part path. A lone file name ("en.html",
        # "products.html") is a locale homepage/section too.
        if "." not in seg and not re.search(r"\d", seg) and len(seg) <= 40:
            return f"single clean segment '{seg}'"
    return None


def is_landing_url(url):
    """True for site roots and section/landing pages rather than single articles.

    Search engines answer generic "top news" queries with outlet homepages and
    section pages (``/``, ``/technology/``, ``/section/technology``). Such a URL
    can never back a specific claim, so it is only useful as a citation when
    the same search returned nothing better (see ``collect_citations``).
    Promotional/ad pages are treated as landing pages too — they can never be
    a news citation, so every caller drops or strips them the same way.
    """
    return _landing_reason(url) is not None


# Social-media / community-forum hosts. Threads on these can INFORM the model
# (public sentiment, layman explanations) but they are never acceptable as a
# citation in a formal research report — the reference section of a story
# drops them the same way it drops ad/promo links.
_UGC_HOST_SUFFIXES = (
    "reddit.com", "quora.com", "x.com", "twitter.com", "facebook.com",
    "instagram.com", "tiktok.com", "pinterest.com", "tumblr.com",
    "threads.net", "bsky.app", "youtube.com", "vk.com", "weibo.com",
)


def _ugc_reason(url):
    """Return a human-readable reason string if ``url`` is a social/forum post."""
    try:
        host = (urlparse(url).hostname or "").lower()
    except (ValueError, AttributeError):
        return None
    if not host:
        return None
    if host.startswith("www."):
        host = host[4:]
    for suffix in _UGC_HOST_SUFFIXES:
        if host == suffix or host.endswith("." + suffix):
            return f"social/forum host {host}"
    return None


def is_ugc_url(url):
    """True for social-media / community-forum content (Reddit, Quora, X...).

    Search results from these hosts are still shown to the model — public
    discussion is legitimate research input — but :func:`finalize_story`
    strips them from published References, so a formal report never cites a
    comment thread.
    """
    return _ugc_reason(url) is not None


def scrub_search_results(results, query=None):
    """Drop links that could never be a news citation from a search result set.

    Ad/promo URLs (retail domains, ad networks, shop hosts, commerce pages)
    are dropped unconditionally. Homepage/section landing pages are dropped
    only when the same search also returned a deep article-level link, so a
    landing page is still available as a last-resort fallback for generic
    queries that return nothing deeper.

    Every drop/keep decision is logged (``[scrub]``) so a search that "only
    Wikipedia survived" can be traced rule by rule. ``query`` is optional and
    only decorates the summary line.
    """
    incoming = list(results or [])
    kept = []
    has_deep = False
    dropped_ads = 0
    for r in incoming:
        if not isinstance(r, dict):
            print(f"[scrub] DROP non-dict result: {r!r}")
            continue
        url = r.get("url", "")
        if not url:
            print(f"[scrub] DROP result with empty url: {r.get('title', '')!r}")
            continue
        ad = _ad_reason(url)
        if ad:
            print(f"[scrub] DROP ad ({ad}): {url}")
            dropped_ads += 1
            continue
        landing = _landing_reason(url)
        if not landing:
            has_deep = True
        kept.append(r)
    if has_deep:
        survivors = []
        for r in kept:
            url = r.get("url", "")
            reason = _landing_reason(url)
            if reason:
                print(f"[scrub] DROP landing ({reason}): {url}")
            else:
                survivors.append(r)
    else:
        survivors = kept
        if kept:
            print(f"[scrub] no deep links in result set — {len(kept)} landing page(s) kept as fallback")
    total = len(incoming)
    if len(survivors) != total:
        label = f" for query: {query}" if query else ""
        print(f"[scrub] kept {len(survivors)}/{total} links ({dropped_ads} ad, {total - len(survivors) - dropped_ads} landing){label}")
    return survivors
