"""Run one enrichment job. Invoked as a subprocess by api.py.

    python worker.py <job_dir>

job_dir must already contain the uploaded file as `input<ext>`.

One job = one subprocess because Scrapy/Twisted allows a single reactor per
process and it cannot be restarted. Both spiders therefore run inside ONE
CrawlerProcess, chained via deferreds -- not two sequential CrawlerProcess objects,
which would raise ReactorNotRestartable on the second.
"""

import json
import os
import sys
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from scrapy.crawler import CrawlerProcess  # noqa: E402
from scrapy.utils.project import get_project_settings  # noqa: E402

import sheet_io  # noqa: E402
from logo_scraper import settings as base_settings  # noqa: E402


class Progress:
    """Row-level progress, flushed to disk for api.py to read.

    The API cannot see into this process, so state travels through the filesystem.
    """

    def __init__(self, path, total):
        self.path = path
        self.total = max(total, 1)
        self.done = 0
        self.stage = "starting"
        self.flush()

    def set_stage(self, stage, total=None):
        self.stage = stage
        self.done = 0
        if total is not None:
            self.total = max(total, 1)
        self.flush()

    def tick(self):
        self.done += 1
        # Cheap enough at this row count to flush every tick, and it keeps the UI
        # honest if the job dies mid-way.
        self.flush()

    def flush(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"stage": self.stage, "done": self.done, "total": self.total}, fh)
        os.replace(tmp, self.path)


def _find_input(job_dir):
    for f in os.listdir(job_dir):
        if f.startswith("input") and os.path.splitext(f)[1].lower() in (".xlsx", ".xlsm", ".csv"):
            return os.path.join(job_dir, f)
    raise FileNotFoundError("no input file in job dir")


def main(job_dir):
    src = _find_input(job_dir)
    meta = sheet_io.read_sheet(src)
    rows = meta["rows"]

    progress = Progress(os.path.join(job_dir, "progress.json"), len(rows) * 2)
    skip_website = os.environ.get("SKIP_WEBSITE_LOGO") == "1"

    settings = get_project_settings()
    for k in dir(base_settings):
        if k.isupper():
            settings.set(k, getattr(base_settings, k), priority="project")

    process = CrawlerProcess(settings=settings, install_root_handler=False)

    from logo_scraper.spiders.gofan_search import GofanSearchSpider
    from logo_scraper.spiders.website_logo import WebsiteLogoSpider

    progress.set_stage("gofan", len(rows))
    d = process.crawl(GofanSearchSpider, rows=rows, progress=progress)

    if not skip_website:
        # Playwright config must ride on the spider's custom_settings. Passing
        # settings= to process.crawl() does NOT work: crawl() forwards **kwargs to
        # the spider's __init__, so the download handlers would be silently
        # dropped and every playwright request would quietly fall back to a plain
        # fetch. Scoping it to this spider also keeps the GoFan spider -- which
        # hits a JSON API and needs no browser -- from ever launching Chromium.
        WebsiteLogoSpider.custom_settings = base_settings.playwright_settings()

        def _then_website(_):
            progress.set_stage("website", len(rows))
            return process.crawl(WebsiteLogoSpider, rows=rows, progress=progress)

        d.addCallback(_then_website)

    process.start()  # blocks until every chained crawl finishes

    progress.set_stage("writing", 1)
    base = os.path.splitext(os.path.basename(src))[0]
    stem = "enriched"
    out_csv = os.path.join(job_dir, f"{stem}.csv")
    sheet_io.write_csv(out_csv, meta["header"], rows)

    out_xlsx = None
    if meta["ext"] != ".csv":
        out_xlsx = os.path.join(job_dir, f"{stem}.xlsx")
        sheet_io.write_xlsx(src, out_xlsx, meta, rows)

    counts = {
        "rows": len(rows),
        "gofan_matched": sum(1 for r in rows if r.get(sheet_io.GOFAN_URL_COL)),
        "gofan_logos": sum(1 for r in rows if r.get(sheet_io.GOFAN_LOGO_COL)),
        "website_logos": sum(1 for r in rows if r.get(sheet_io.WEBSITE_LOGO_COL)),
    }
    with open(os.path.join(job_dir, "result.json"), "w", encoding="utf-8") as fh:
        json.dump(
            {
                "counts": counts,
                "rows": rows,
                "csv": os.path.basename(out_csv),
                "xlsx": os.path.basename(out_xlsx) if out_xlsx else None,
                "source_name": base,
            },
            fh,
        )
    progress.set_stage("done", 1)
    progress.tick()
    print(json.dumps(counts))


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: worker.py <job_dir>", file=sys.stderr)
        sys.exit(2)
    job_dir = sys.argv[1]
    try:
        main(job_dir)
    except Exception as exc:  # surface the reason to the API instead of a bare exit code
        with open(os.path.join(job_dir, "error.txt"), "w", encoding="utf-8") as fh:
            fh.write(f"{exc}\n\n{traceback.format_exc()}")
        traceback.print_exc()
        sys.exit(1)
