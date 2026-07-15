"""Resolve each row to a GoFan school via the search API.

Fills `gofan url` and `gofan logo url`.

The ladder is walked by yielding the next request from the callback rather than
looping with blocking I/O. DD-Scrapper's equivalent calls urllib + time.sleep()
inside start_requests(), which stalls the Twisted reactor for thousands of serial
round-trips before a single Scrapy request goes out. This costs nothing to avoid.
"""

import json

import scrapy

import gofan_match as gm
from sheet_io import ADDRESS_COL, GOFAN_LOGO_COL, GOFAN_URL_COL, NAME_COL


class GofanSearchSpider(scrapy.Spider):
    name = "gofan_search"

    def __init__(self, rows=None, progress=None, *a, **kw):
        super().__init__(*a, **kw)
        self.rows = rows or []
        self._progress = progress
        self.matched = 0
        self.unmatched = 0
        self.placeholders = 0

    def start_requests(self):
        for i, row in enumerate(self.rows):
            # Pre-fill so an errored request degrades to blank instead of a
            # missing key downstream.
            row.setdefault(GOFAN_URL_COL, "")
            row.setdefault(GOFAN_LOGO_COL, "")

            name = (row.get(NAME_COL) or "").strip()
            city, state, zip_code = gm.parse_address(row.get(ADDRESS_COL))
            if not name or not state:
                self._done(i, None)
                continue

            ladder = gm.query_ladder(name, city=city, state=state)
            if not ladder:
                self._done(i, None)
                continue
            yield self._step(i, ladder, 0, city, state, zip_code)

    def _step(self, idx, ladder, pos, city, state, zip_code):
        return scrapy.Request(
            gm.search_url(ladder[pos]),
            callback=self.parse_search,
            errback=self.errback,
            dont_filter=True,
            cb_kwargs={
                "idx": idx,
                "ladder": ladder,
                "pos": pos,
                "city": city,
                "state": state,
                "zip_code": zip_code,
            },
            headers={"Accept": "application/json"},
            meta={"download_timeout": 25},
        )

    def parse_search(self, response, idx, ladder, pos, city, state, zip_code):
        candidates = []
        if response.status == 200:
            try:
                payload = json.loads(response.text)
                # Fail loudly on a shape change rather than silently writing blanks.
                if isinstance(payload, list):
                    candidates = payload
                elif isinstance(payload, dict) and isinstance(payload.get("content"), list):
                    candidates = payload["content"]
                else:
                    self.logger.warning(
                        "Unexpected GoFan search payload for %r: %.120s",
                        ladder[pos],
                        response.text,
                    )
            except ValueError:
                self.logger.warning("Non-JSON from GoFan for %r", ladder[pos])

        hit = gm.pick(
            candidates, city, state, zip_code, strict=gm.is_strict_query(ladder[pos])
        )
        if hit:
            self._done(idx, hit)
            return

        if pos + 1 < len(ladder):
            yield self._step(idx, ladder, pos + 1, city, state, zip_code)
        else:
            self._done(idx, None)

    def errback(self, failure):
        kw = failure.request.cb_kwargs
        idx, ladder, pos = kw["idx"], kw["ladder"], kw["pos"]
        self.logger.debug("GoFan request failed for %r: %s", ladder[pos], failure.value)
        if pos + 1 < len(ladder):
            yield self._step(idx, ladder, pos + 1, kw["city"], kw["state"], kw["zip_code"])
        else:
            self._done(idx, None)

    def _done(self, idx, hit):
        row = self.rows[idx]
        if hit:
            logo = gm.encode_logo_url(hit.get("logoUrl"))
            row[GOFAN_URL_COL] = gm.school_url(hit.get("huddleId"))
            row[GOFAN_LOGO_COL] = logo
            self.matched += 1
            if gm.is_placeholder_logo(logo):
                self.placeholders += 1
            self.logger.info(
                "matched %-46s -> %s (%s)",
                (row.get(NAME_COL) or "")[:46],
                hit.get("huddleId"),
                hit.get("name"),
            )
        else:
            self.unmatched += 1
            self.logger.info("no GoFan match for %s", row.get(NAME_COL))
        if self._progress:
            self._progress.tick()
