"""Measure peak RSS of the website-logo stage.

Answers one question: does this fit on a 512 MB Render box?

    .venv/bin/python tests/measure_memory.py          # default settings
    LOW_MEMORY=1 .venv/bin/python tests/measure_memory.py

Reports the peak combined RSS of the worker and every Chromium child, which is
what Render's cgroup limit actually accounts for.
"""

import os
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

ROWS = int(os.environ.get("MEASURE_ROWS", "6"))


def _rss_kb(pids):
    if not pids:
        return 0
    out = subprocess.run(
        ["ps", "-o", "rss=", "-p", ",".join(str(p) for p in pids)],
        capture_output=True,
        text=True,
    ).stdout
    return sum(int(x) for x in out.split() if x.strip().isdigit())


def _descendants(root):
    out = subprocess.run(["ps", "-eo", "pid=,ppid="], capture_output=True, text=True).stdout
    kids = {}
    for line in out.strip().splitlines():
        parts = line.split()
        if len(parts) == 2:
            kids.setdefault(int(parts[1]), []).append(int(parts[0]))
    seen, stack = [], [root]
    while stack:
        p = stack.pop()
        seen.append(p)
        stack.extend(kids.get(p, []))
    return seen


def main():
    peak = {"kb": 0, "procs": 0}
    stop = threading.Event()
    me = os.getpid()

    def sample():
        while not stop.is_set():
            pids = _descendants(me)
            kb = _rss_kb(pids)
            if kb > peak["kb"]:
                peak["kb"] = kb
                peak["procs"] = len(pids)
            time.sleep(0.25)

    t = threading.Thread(target=sample, daemon=True)
    t.start()

    from scrapy.crawler import CrawlerProcess

    import sheet_io
    from logo_scraper import settings as bs
    from logo_scraper.spiders.website_logo import WebsiteLogoSpider

    src = os.environ.get(
        "MEASURE_SHEET",
        os.path.join(os.path.dirname(HERE), "Duval_County_Middle_Schools_Directory.xlsx"),
    )
    meta = sheet_io.read_sheet(src)
    rows = meta["rows"][:ROWS]

    st = {k: getattr(bs, k) for k in dir(bs) if k.isupper()}
    st.update(bs.playwright_settings())
    st["LOG_LEVEL"] = "ERROR"
    WebsiteLogoSpider.custom_settings = None

    t0 = time.time()
    p = CrawlerProcess(settings=st)
    p.crawl(WebsiteLogoSpider, rows=rows)
    p.start()
    elapsed = time.time() - t0

    stop.set()
    time.sleep(0.4)

    got = sum(1 for r in rows if r.get(sheet_io.WEBSITE_LOGO_COL))
    mb = peak["kb"] / 1024
    mode = "LOW_MEMORY" if os.environ.get("LOW_MEMORY") == "1" else "default"
    pages = st["PLAYWRIGHT_MAX_PAGES_PER_CONTEXT"]

    print("\n" + "=" * 62)
    print(f"  mode          : {mode}  (pages={pages})")
    print(f"  rows          : {got}/{len(rows)} logos in {elapsed:.0f}s")
    print(f"  PEAK RSS      : {mb:.0f} MB   across {peak['procs']} processes")
    print(f"  512MB budget  : {'FITS' if mb < 430 else 'TOO BIG'}"
          f"   (worker+chromium only; uvicorn adds ~60-90MB on the box)")
    print("=" * 62)


if __name__ == "__main__":
    main()
