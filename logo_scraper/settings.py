"""Shared Scrapy settings.

Extracted into a real module rather than imported from an enrichment script --
DD-Scrapper keeps its equivalent factory inside `enrich_website_name.py`, a module
that is otherwise dead code, and every other spider imports it from there.
"""

import os

BOT_NAME = "logo_scraper"
SPIDER_MODULES = ["logo_scraper.spiders"]
NEWSPIDER_MODULE = "logo_scraper.spiders"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

ROBOTSTXT_OBEY = False

# 4xx/5xx must reach parse() so the row falls back to blank instead of being
# dropped -- a missing logo is a valid outcome, not a crawl failure.
HTTPERROR_ALLOW_ALL = True

CONCURRENT_REQUESTS = 8
CONCURRENT_REQUESTS_PER_DOMAIN = 4
DOWNLOAD_DELAY = 0.25
DOWNLOAD_TIMEOUT = 30
RETRY_ENABLED = True
RETRY_TIMES = 2
RETRY_HTTP_CODES = [429, 500, 502, 503, 504, 408, 522, 524]

AUTOTHROTTLE_ENABLED = True
AUTOTHROTTLE_START_DELAY = 0.5
AUTOTHROTTLE_MAX_DELAY = 15.0
AUTOTHROTTLE_TARGET_CONCURRENCY = 4.0

COOKIES_ENABLED = False
# Errors continuously on Render's network; the extension is also removed outright.
TELNETCONSOLE_ENABLED = False
EXTENSIONS = {"scrapy.extensions.telnet.TelnetConsole": None}

LOG_LEVEL = os.environ.get("SCRAPY_LOG_LEVEL", "INFO")
TWISTED_REACTOR = "twisted.internet.asyncioreactor.AsyncioSelectorReactor"
FEED_EXPORT_ENCODING = "utf-8"

# Graceful self-close that still flushes partial results, sitting below api.py's
# hard watchdog. See render.yaml for how the timeout budget is layered.
CLOSESPIDER_TIMEOUT = int(os.environ.get("CLOSESPIDER_TIMEOUT", "1800"))
MEMUSAGE_ENABLED = True
MEMUSAGE_LIMIT_MB = int(os.environ.get("MEMUSAGE_LIMIT_MB", "1400"))
MEMUSAGE_WARNING_MB = int(os.environ.get("MEMUSAGE_WARNING_MB", "1100"))


# We need the DOM to read the logo's URL -- never the bytes it points at. A school
# homepage otherwise pulls ~800 requests of images, ads and analytics per row,
# which is what pushes navigation past its timeout.
_BLOCKED_RESOURCE_TYPES = {"image", "media", "font", "stylesheet"}
_BLOCKED_HOST_MARKERS = (
    "doubleclick",
    "googlesyndication",
    "googletagmanager",
    "google-analytics",
    "googleadservices",
    "adtrafficquality",
    "segment.io",
    "segment.com",
    "nr-data.net",
    "newrelic",
    "hotjar",
    "facebook.net",
    "facebook.com/tr",
    "launchdarkly",
    "onetrust",
    "cookielaw",
    "hightouch-events",
    "stripe.com",
    "recaptcha",
)


def _should_abort(request):
    if request.resource_type in _BLOCKED_RESOURCE_TYPES:
        return True
    url = request.url.lower()
    return any(m in url for m in _BLOCKED_HOST_MARKERS)


def playwright_settings():
    """Overrides for the website-logo spider only.

    Kept off the module defaults so the GoFan spider -- which hits a plain JSON
    API and needs no browser -- never pays Chromium's startup cost.
    """
    # LOW_MEMORY targets a 512 MB box (Render free/starter). Chromium is the whole
    # memory story here: on a 512 MB instance the default 4 pages gets the CONTAINER
    # OOM-killed the moment the browser launches -- the API restarts, in-flight jobs
    # die, and the client sees the job vanish.
    low_memory = os.environ.get("LOW_MEMORY") == "1"

    # The first lever to pull. Each open page is ~150-250 MB.
    _pages = int(os.environ.get("PLAYWRIGHT_MAX_PAGES", "1" if low_memory else "4"))

    args = ["--no-sandbox", "--disable-dev-shm-usage"]

    if low_memory:
        args += [
            # One process for browser+renderer instead of a process per tab. This is
            # the single biggest saving, and it is why LOW_MEMORY is opt-in:
            # --single-process is explicitly unsupported by Playwright and can make
            # Chromium unstable. Acceptable trade on a box that otherwise cannot run
            # a browser at all; never enable it where you have the RAM.
            "--single-process",
            "--no-zygote",
            "--renderer-process-limit=1",
            "--disable-gpu",
            "--disable-extensions",
            "--disable-background-networking",
            "--disable-sync",
            "--disable-translate",
            "--mute-audio",
            # Cap V8's heap. We only read the DOM; nothing here needs a big heap.
            "--js-flags=--max-old-space-size=128",
            # Belt and braces with PLAYWRIGHT_ABORT_REQUEST: never decode an image.
            "--blink-settings=imagesEnabled=false",
        ]

    # Local-dev escape hatch, unset in production. Some ISP/router resolvers
    # SERVFAIL on school domains (duvalschools.org does on the dev machine here),
    # which fails the crawl for reasons that have nothing to do with the code.
    # Example: PLAYWRIGHT_HOST_RESOLVER_RULES="MAP *.duvalschools.org 151.101.194.37"
    # Note the wildcard: Chromium ignores a bare-host MAP rule for this domain.
    resolver_rules = os.environ.get("PLAYWRIGHT_HOST_RESOLVER_RULES")
    if resolver_rules:
        args += [
            f"--host-resolver-rules={resolver_rules}",
            "--disable-features=AsyncDNS,DnsOverHttps",
        ]

    launch = {"headless": True, "args": args}
    if low_memory:
        # The headless-shell binary is ~197 MB on disk vs ~356 MB for full headless
        # Chrome, and correspondingly lighter at runtime. It has no UI surface we
        # use -- we only need the DOM.
        launch["channel"] = "chromium-headless-shell"

    return {
        "DOWNLOAD_HANDLERS": {
            "http": "scrapy_playwright.handler.ScrapyPlaywrightDownloadHandler",
            "https": "scrapy_playwright.handler.ScrapyPlaywrightDownloadHandler",
        },
        "PLAYWRIGHT_BROWSER_TYPE": "chromium",
        "PLAYWRIGHT_LAUNCH_OPTIONS": launch,
        "PLAYWRIGHT_ABORT_REQUEST": _should_abort,
        "PLAYWRIGHT_DEFAULT_NAVIGATION_TIMEOUT": int(
            os.environ.get("PLAYWRIGHT_NAV_TIMEOUT_MS", "60000")
        ),
        # Chromium is memory-hungry; more than a couple of pages at once will OOM
        # a 2 GB Render box.
        "PLAYWRIGHT_MAX_CONTEXTS": 1,
        "PLAYWRIGHT_MAX_PAGES_PER_CONTEXT": _pages,
        "CONCURRENT_REQUESTS": _pages,
        # A directory is usually one district = one domain, so the per-domain cap
        # IS the real concurrency limit. Leaving it at the default throttles the
        # whole stage down to a couple of pages at a time.
        "CONCURRENT_REQUESTS_PER_DOMAIN": _pages,
        # AutoThrottle must be off here. It derives its delay from response
        # latency, and a rendered page legitimately takes ~5s -- which it reads as
        # a struggling server and backs off from, compounding to ~55s/row. The
        # open page count is already the rate limit.
        "AUTOTHROTTLE_ENABLED": False,
        "DOWNLOAD_DELAY": 0,
        # A DNS/connection failure is not worth 2 more attempts before the
        # browser fallback, which is the thing that actually works.
        "RETRY_TIMES": 0,
    }
