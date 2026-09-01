"""URL classification shared by the web-search tool and the story pipeline.

The ``web_search`` tool scrubs its results before they enter the LLM context
(and before they are recorded as ``_search_details``), and ``self-chat.py``
uses the same rules to keep ad/promo and landing-page URLs out of citations
and the published story. Keeping the classifier here with no server-side
dependencies gives both sides one consistent definition.
"""

import re
from urllib.parse import urlparse

_LANDING_SEGMENT_RE = re.compile(
    r"^(section|sections|category|categories|topics?|tags?|archive|index)$"
)

# URL path segments that mark a site-internal search/category/listing page —
# never an individual article ("flipkart.com/q/fashion-tops",
# "nykaafashion.com/women/tops/c/4497", "meesho.com/tops-ladies/pl/3ja").
_STRUCTURAL_PATH_SEGMENTS = {
    "q", "s", "c", "pl", "pr", "p", "products", "product", "category",
    "categories", "collection", "collections", "shop", "store", "search",
    "browse", "listings", "listing", "tagged",
}

# URL path segments that mark promotional pages — shopping cart/checkout,
# product listings, driver/software downloads, stock quotes, app-store detail
# pages, and company/profile pages. Such a URL can never back a news claim;
# it is dropped from citations and stripped from the story body.
_AD_PATH_SEGMENTS = {
    "account", "apps", "bag", "basket", "career", "careers", "cart",
    "catalog", "catalogue", "categories", "category", "checkout",
    "collections", "collection", "coupon", "coupons",
    "deal", "deals", "detail", "details", "discount", "dictionary",
    "download", "downloads", "driver", "drivers", "home", "item", "items",
    "jobs", "login", "offer", "offers", "order", "orders", "pdp", "plp",
    "portfolio", "price", "prices", "pricing", "product", "products", "promo",
    "promotions", "quote", "quotes", "register", "sale", "seller", "shop",
    "shopping", "signin", "signup", "sku", "stock", "store", "vendor",
    "wishlist",
}

# Domain suffix list for domains that are pure retail/marketplace/classified
# sites — every page there is a product or listing page, never an article.
_AD_DOMAIN_SUFFIXES = (
    "zara.com", "zara.co.in", "andindia.com", "myntra.com", "flipkart.com",
    "amazon.com", "amazon.in", "amazon.co.uk", "ajio.com", "meesho.com",
    "nykaa.com", "snapdeal.com", "shopclues.com", "ebay.com", "etsy.com",
    "alibaba.com", "aliexpress.com", "walmart.com", "target.com", "bestbuy.com",
    "homedepot.com", "asos.com", "shein.com", "temu.com", "croma.com",
    "tatacliq.com", "reliancedigital.in", "jabong.com", "koovs.com",
)

# URL patterns that mark product-list/product-detail or reference pages:
# an Amazon/Walmart product "dp/item" page, a dictionary entry, a stock
# symbol lookup, or a company-profile page. NOTE: bare numeric article IDs
# and date-prefixed slugs (/2026/09/01/...) must NOT be treated as ad links —
# those are the shape of a real article URL.
_AD_URL_RE = re.compile(
    r"(?:/dp/|/itm/|/dictionary/|/\d+-\d+-\d+-/|\bsku\s*=\s*[A-Z0-9-]+)", re.I
)
_AD_PROFILE_HOSTS = ("linkedin.com",)


def is_ad_url(url):
    """True for promotional shopping/download/quote/app-store URLs.

    News citations must be article pages; a product listing, driver download,
    stock quote, or company profile never is. Catches commercial URLs the
    structural-segment check misses, e.g. Zara category pages of the form
    ``/in/en/s-woman-tops-l10056.html`` and marketplace product IDs on pages
    whose path has no obvious keyword.
    """
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        path = parsed.path or "/"
    except (ValueError, AttributeError):
        return False
    if not host:
        return False
    if any(host == s or host.endswith("." + s) for s in _AD_DOMAIN_SUFFIXES):
        return True
    if any(host == s or host.endswith("." + s) for s in _AD_PROFILE_HOSTS):
        if any(seg.startswith("company") for seg in path.split("/") if seg):
            return True
        return False
    if _AD_URL_RE.search(url):
        return True
    segs = [seg.split(".")[0].lower() for seg in path.split("/") if seg]
    if any(seg in _AD_PATH_SEGMENTS for seg in segs):
        return True
    return False


def is_landing_url(url):
    """True for site roots and section/landing pages rather than single articles.

    Search engines answer generic "top news" queries with outlet homepages and
    section pages (``/``, ``/technology/``, ``/section/technology``). Such a URL
    can never back a specific claim, so it is only useful as a citation when
    the same search returned nothing better (see ``collect_citations``).
    Promotional/ad pages are treated as landing pages too — they can never be
    a news citation, so every caller drops or strips them the same way.
    """
    try:
        parsed = urlparse(url)
        path = parsed.path or "/"
    except (ValueError, AttributeError):
        return False
    path = path.rstrip("/")
    if not path:
        return True
    segs = [s for s in path.split("/") if s]
    if not segs:
        return True
    if is_ad_url(url):
        return True
    if any(seg.split(".")[0].lower() in _STRUCTURAL_PATH_SEGMENTS for seg in segs):
        return True
    if len(segs) == 1:
        seg = segs[0].lower()
        # One clean segment with no file extension and no digits ("/technology")
        # is a section page; real article slugs almost always carry a date, an
        # ID, or a hyphenated multi-part path. A lone file name ("en.html",
        # "products.html") is a locale homepage/section too.
        if "." not in seg and not re.search(r"\d", seg) and len(seg) <= 40:
            return True
    if _LANDING_SEGMENT_RE.match(segs[0].split(".")[0].lower()):
        return True
    return False


def scrub_search_results(results):
    """Drop links that could never be a news citation from a search result set.

    Ad/promo URLs (shopping, downloads, quotes, app stores, company profiles)
    are dropped unconditionally. Homepage/section landing pages are dropped
    only when the same search also returned a deep article-level link, so a
    landing page is still available as a last-resort fallback for generic
    queries that return nothing deeper.
    """
    kept = []
    has_deep = False
    for r in results or []:
        if not isinstance(r, dict):
            continue
        url = r.get("url", "")
        if not url:
            continue
        if is_ad_url(url):
            continue
        if not is_landing_url(url):
            has_deep = True
        kept.append(r)
    if not has_deep:
        return kept
    return [r for r in kept if not is_landing_url(r.get("url", ""))]