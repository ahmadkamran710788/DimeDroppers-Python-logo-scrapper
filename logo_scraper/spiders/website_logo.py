"""Scrape each row's Official Website for the school's logo.

Fills `official website logo`.

Every row of the Duval sheet points at www.duvalschools.org, which serves a
JavaScript "Client Challenge" to non-browser clients -- every path on the domain,
including /favicon.ico, returns the same ~3 KB challenge page. So a plain fetch is
tried first (cheap, and correct for ordinary school sites), and only rows that come
back challenged or logo-less are retried through scrapy-playwright.

Once rendered, the logo is the header's home link image, e.g.
https://cmsv2-assets.apptegy.net/uploads/24463/logo/27276/Mandarin_Middle_School_Logo.png
"""

import re

import scrapy
from scrapy_playwright.page import PageMethod

from sheet_io import WEBSITE_COL, WEBSITE_LOGO_COL

# Signature of the bot-check interstitial: HTTP 200, no real markup.
_CHALLENGE_MARKERS = (
    "client challenge",
    "javascript is disabled in your browser",
    "enable javascript to proceed",
    "checking your browser",
    "just a moment",
)

# og:image is often a generic district graphic, and favicons are district-wide, so
# the header logo is tried first and the weaker sources only as fallbacks.
_LOGO_XPATHS = (
    '//a[@href]//img[contains(translate(@alt,"RETURNHOM","returnhom"),"return to home")]/@src',
    '//header//img[contains(translate(@class,"LOGO","logo"),"logo")]/@src',
    '//header//img[contains(translate(@alt,"LOGO","logo"),"logo")]/@src',
    '//img[contains(translate(@class,"LOGO","logo"),"logo")]/@src',
    '//img[contains(translate(@alt,"LOGO","logo"),"logo")]/@src',
    '//img[contains(translate(@src,"LOGO","logo"),"logo")]/@src',
    '//meta[@property="og:image"]/@content',
    '//meta[@name="og:image"]/@content',
    '//link[contains(translate(@rel,"APPLETOUCHICON","appletouchicon"),"apple-touch-icon")]/@href',
    '//link[contains(translate(@rel,"ICON","icon"),"icon")]/@href',
)

# Tracking pixels, spacers and sprites masquerading as logos.
_JUNK_SUBSTRINGS = (
    "data:image/gif",
    "spacer.gif",
    "pixel.gif",
    "1x1.",
    "blank.gif",
    "transparent.png",
    "/ads/",
    "doubleclick",
    "google-analytics",
    "facebook.com/tr",
)


def is_challenge(response):
    """Did we get the bot-check page instead of the site?"""
    ctype = (response.headers.get("Content-Type") or b"").decode("latin-1").lower()
    if "html" not in ctype:
        return False
    body = response.text[:8000].lower()
    if any(m in body for m in _CHALLENGE_MARKERS):
        return True
    # A "real" school homepage that is a few KB with no images at all is the
    # challenge in disguise.
    return len(response.body) < 6000 and "<img" not in body


class WebsiteLogoSpider(scrapy.Spider):
    name = "website_logo"

    def __init__(self, rows=None, progress=None, *a, **kw):
        super().__init__(*a, **kw)
        self.rows = rows or []
        self._progress = progress
        self.found = 0
        self.missing = 0
        self.rendered = 0

    def start_requests(self):
        for i, row in enumerate(self.rows):
            row.setdefault(WEBSITE_LOGO_COL, "")
            url = (row.get(WEBSITE_COL) or "").strip()
            if not url.startswith(("http://", "https://")):
                self._done(i, None)
                continue
            yield self._request(url, i, render=False)

    def _request(self, url, idx, render):
        meta = {"download_timeout": 90 if render else 25}
        if render:
            meta.update(
                {
                    "playwright": True,
                    # Deliberately NOT networkidle: these pages carry ad/analytics
                    # beacons that never settle, so networkidle burns the full
                    # timeout on every row. The challenge has already resolved by
                    # the time the real document fires domcontentloaded (~5s);
                    # waiting for an <img> confirms the real page is in.
                    "playwright_page_goto_kwargs": {"wait_until": "domcontentloaded"},
                    "playwright_page_methods": [
                        PageMethod("wait_for_selector", "img", timeout=30000)
                    ],
                }
            )
        return scrapy.Request(
            url,
            callback=self.parse,
            errback=self.errback,
            dont_filter=True,
            cb_kwargs={"idx": idx, "rendered": render},
            meta=meta,
        )

    def parse(self, response, idx, rendered):
        logo = None
        challenged = is_challenge(response)
        if response.status == 200 and not challenged:
            logo = self._extract(response)

        if logo:
            self._done(idx, logo)
            return

        # One escalation only: plain fetch failed, retry rendered.
        if not rendered:
            self.rendered += 1
            self.logger.info(
                "%s -> %s; retrying with browser",
                response.url,
                "challenge page" if challenged else f"no logo (HTTP {response.status})",
            )
            yield self._request(response.url, idx, render=True)
            return

        self._done(idx, None)

    def _extract(self, response):
        for xp in _LOGO_XPATHS:
            for raw in response.xpath(xp).getall():
                url = (raw or "").strip()
                if not url or url.startswith("data:"):
                    continue
                if any(j in url.lower() for j in _JUNK_SUBSTRINGS):
                    continue
                absolute = response.urljoin(url)
                if re.match(r"^https?://", absolute):
                    return absolute
        return None

    def errback(self, failure):
        kw = failure.request.cb_kwargs
        idx, rendered = kw["idx"], kw["rendered"]
        if not rendered:
            self.rendered += 1
            self.logger.info("%s failed (%s); retrying with browser", failure.request.url, failure.value)
            yield self._request(failure.request.url, idx, render=True)
            return
        self.logger.warning("website logo failed for row %s: %s", idx, failure.value)
        self._done(idx, None)

    def _done(self, idx, logo):
        self.rows[idx][WEBSITE_LOGO_COL] = logo or ""
        if logo:
            self.found += 1
        else:
            self.missing += 1
        if self._progress:
            self._progress.tick()
