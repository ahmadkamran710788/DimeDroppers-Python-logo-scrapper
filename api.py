"""FastAPI job service: upload a directory sheet -> enriched xlsx/csv.

Disk is the source of truth for job state, not the in-memory JOBS dict.

The original port from DD-Scrapper kept jobs purely in memory and wiped JOBS_DIR
on startup. That made any restart -- a Render redeploy, a spin-down, or uvicorn
--reload noticing a .py edit -- silently 404 every in-flight job AND delete the
running worker's output directory out from under it. The worker already writes
everything needed to reconstruct state (progress.json / result.json / error.txt),
so startup now rebuilds JOBS from job.json instead of destroying it, and a job
survives the restart of the process that launched it.

JOBS is still process-local: run exactly ONE uvicorn worker. Rebuilding from disk
makes jobs survive *sequential* restarts, it does not share them across workers.
"""

import json
import os
import shutil
import signal
import subprocess
import sys
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

import sheet_io

HERE = os.path.dirname(os.path.abspath(__file__))
JOBS_DIR = os.environ.get("JOBS_DIR") or os.path.join(HERE, "jobs")

MAX_CONCURRENT_JOBS = int(os.environ.get("MAX_CONCURRENT_JOBS", "1"))
JOB_MAX_RUNTIME_SECONDS = int(os.environ.get("JOB_MAX_RUNTIME_SECONDS", "3600"))
MAX_UPLOAD_BYTES = int(os.environ.get("MAX_UPLOAD_BYTES", str(10 * 1024 * 1024)))
JOB_RETENTION_HOURS = int(os.environ.get("JOB_RETENTION_HOURS", "24"))
ALLOWED_EXT = (".xlsx", ".xlsm", ".csv")

RESTART_ERROR = "the server restarted while this job was running; please run it again"

# job_id -> {status, started_at, error, filename, row_count, pid, proc}
# `proc` is the live Popen when THIS process launched the job; it is None for jobs
# rebuilt from disk after a restart, which is why status must never depend on it.
JOBS = {}


def _job_dir(job_id):
    return os.path.join(JOBS_DIR, job_id)


def _read_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return default


def _write_json(path, payload):
    """Atomic, so a reader never sees a half-written job.json."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh)
    os.replace(tmp, path)


def _pid_alive(pid):
    """Is the worker still running?

    Signal 0 does no work but still performs the permission/existence check.
    """
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, ValueError):
        return False
    except PermissionError:
        # Exists, owned by someone else. Can't be our worker.
        return False
    return True


def _load_jobs_from_disk():
    """Rebuild JOBS after a restart instead of wiping it.

    Without this, a redeploy or a `--reload` triggered by a .py edit turns every
    in-flight job into a 404 the client can never recover from.
    """
    if not os.path.isdir(JOBS_DIR):
        return
    cutoff = time.time() - JOB_RETENTION_HOURS * 3600
    for job_id in os.listdir(JOBS_DIR):
        d = _job_dir(job_id)
        meta = _read_json(os.path.join(d, "job.json"))
        if not os.path.isdir(d):
            continue
        if not meta:
            # No metadata to reconstruct from; only reap it once it is clearly old,
            # so a job mid-write during a crash isn't deleted from under itself.
            if os.path.getmtime(d) < cutoff:
                shutil.rmtree(d, ignore_errors=True)
            continue
        if meta.get("started_at", 0) < cutoff:
            shutil.rmtree(d, ignore_errors=True)
            continue
        JOBS[job_id] = {
            "status": "running",  # _refresh resolves this from disk on first read
            "started_at": meta.get("started_at", time.time()),
            "error": None,
            "filename": meta.get("filename"),
            "row_count": meta.get("row_count", 0),
            "pid": meta.get("pid"),
            "proc": None,
        }


@asynccontextmanager
async def lifespan(_app):
    os.makedirs(JOBS_DIR, exist_ok=True)
    # Never rmtree JOBS_DIR here: on a --reload or redeploy the previous worker is
    # often still alive, and deleting its directory destroys the job it is midway
    # through writing. Stale dirs are swept by age instead.
    _load_jobs_from_disk()
    yield


app = FastAPI(title="DD Logo Scraper", lifespan=lifespan)

_origin = os.environ.get("FRONTEND_ORIGIN", "*")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"] if _origin == "*" else [o.strip() for o in _origin.split(",")],
    allow_methods=["*"],
    allow_headers=["*"],
)


def _stop_worker(job):
    """Terminate the worker whether or not we own its handle."""
    proc = job.get("proc")
    if proc is not None and proc.poll() is None:
        proc.terminate()
        return
    pid = job.get("pid")
    if _pid_alive(pid):
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass


def _worker_alive(job):
    proc = job.get("proc")
    if proc is not None:
        return proc.poll() is None
    return _pid_alive(job.get("pid"))


def _refresh(job_id):
    """Resolve a job's status from what is on disk.

    Precedence matters. The worker's own output wins over process liveness, so a
    job that finished while the API was restarting is still reported `done`
    rather than "interrupted". This must work for jobs with no Popen handle --
    that is exactly the rebuilt-after-restart case the old code short-circuited on.
    """
    job = JOBS.get(job_id)
    if not job or job["status"] != "running":
        return job

    d = _job_dir(job_id)

    # 1. Finished output is authoritative.
    if os.path.exists(os.path.join(d, "result.json")):
        job["status"] = "done"
        return job

    # 2. The worker recorded a failure.
    err_path = os.path.join(d, "error.txt")
    if os.path.exists(err_path):
        job["status"] = "error"
        try:
            with open(err_path, encoding="utf-8") as fh:
                job["error"] = fh.read().strip().splitlines()[0][:400]
        except Exception:
            job["error"] = "the job failed"
        return job

    # 3. Still working.
    if _worker_alive(job):
        if time.time() - job["started_at"] > JOB_MAX_RUNTIME_SECONDS:
            _stop_worker(job)
            job["status"] = "error"
            job["error"] = "job exceeded the maximum runtime and was stopped"
        return job

    # 4. Worker gone with nothing to show: killed, OOMed, or lost to a restart.
    job["status"] = "error"
    job["error"] = RESTART_ERROR
    return job


def _refresh_all():
    # Run before the concurrency check so finished-but-unpolled jobs free their slot.
    for jid in list(JOBS):
        _refresh(jid)


@app.get("/health")
def health():
    return {"ok": True}


@app.post("/jobs")
async def create_job(file: UploadFile = File(...)):
    _refresh_all()
    active = sum(1 for j in JOBS.values() if j["status"] == "running")
    if active >= MAX_CONCURRENT_JOBS:
        raise HTTPException(429, "a job is already running; try again shortly")

    ext = os.path.splitext(file.filename or "")[1].lower()
    if ext not in ALLOWED_EXT:
        raise HTTPException(422, f"unsupported file type {ext or '(none)'}; upload .xlsx or .csv")

    body = await file.read()
    if len(body) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, f"file too large (max {MAX_UPLOAD_BYTES // 1024 // 1024} MB)")
    if not body:
        raise HTTPException(422, "uploaded file is empty")

    job_id = uuid.uuid4().hex
    out_dir = _job_dir(job_id)
    os.makedirs(out_dir, exist_ok=True)
    src = os.path.join(out_dir, f"input{ext}")
    with open(src, "wb") as fh:
        fh.write(body)

    # Parse before spawning: a sheet without a findable header is a 422, not a
    # job that fails two minutes later.
    try:
        meta = sheet_io.read_sheet(src)
    except sheet_io.SheetError as exc:
        shutil.rmtree(out_dir, ignore_errors=True)
        raise HTTPException(422, str(exc))
    if not meta["rows"]:
        shutil.rmtree(out_dir, ignore_errors=True)
        raise HTTPException(422, "no data rows found under the header")

    started_at = time.time()
    proc = subprocess.Popen([sys.executable, "worker.py", out_dir], cwd=HERE)
    JOBS[job_id] = {
        "status": "running",
        "started_at": started_at,
        "error": None,
        "filename": file.filename,
        "row_count": len(meta["rows"]),
        "pid": proc.pid,
        "proc": proc,
    }

    # Persist before returning: this is what lets a restart pick the job back up.
    # filename in particular has no other home, and GET /download needs it to name
    # the result after what the user actually uploaded.
    _write_json(
        os.path.join(out_dir, "job.json"),
        {
            "job_id": job_id,
            "filename": file.filename,
            "row_count": len(meta["rows"]),
            "started_at": started_at,
            "pid": proc.pid,
        },
    )
    return {"job_id": job_id, "status": "running", "row_count": len(meta["rows"])}


@app.get("/jobs/{job_id}")
def job_status(job_id: str):
    job = _refresh(job_id)
    if not job:
        raise HTTPException(404, "unknown job")
    progress = _read_json(os.path.join(_job_dir(job_id), "progress.json"), {}) or {}
    result = _read_json(os.path.join(_job_dir(job_id), "result.json"), {}) or {}
    return {
        "job_id": job_id,
        "status": job["status"],
        "filename": job["filename"],
        "row_count": job["row_count"],
        "progress": {
            "stage": progress.get("stage"),
            "done": progress.get("done", 0),
            "total": progress.get("total", job["row_count"]),
        },
        "counts": result.get("counts"),
        "error": job["error"],
    }


@app.get("/jobs/{job_id}/results")
def job_results(job_id: str):
    job = _refresh(job_id)
    if not job:
        raise HTTPException(404, "unknown job")
    if job["status"] != "done":
        raise HTTPException(409, f"job is {job['status']}")
    result = _read_json(os.path.join(_job_dir(job_id), "result.json"))
    if not result:
        raise HTTPException(500, "results missing")
    return {"rows": result["rows"], "counts": result["counts"]}


@app.get("/jobs/{job_id}/download")
def job_download(job_id: str, format: str = Query("xlsx", pattern="^(xlsx|csv)$")):
    job = _refresh(job_id)
    if not job:
        raise HTTPException(404, "unknown job")
    if job["status"] != "done":
        raise HTTPException(409, f"job is {job['status']}")

    result = _read_json(os.path.join(_job_dir(job_id), "result.json")) or {}
    fname = result.get(format)
    if not fname:
        raise HTTPException(404, f"no {format} output for this job (was the upload a CSV?)")

    path = os.path.join(_job_dir(job_id), fname)
    if not os.path.exists(path):
        raise HTTPException(404, "output file missing")

    # Name the download after what the user uploaded. The worker only ever sees
    # the normalised "input.xlsx", so its own stem would read "input_enriched".
    stem = os.path.splitext(os.path.basename(job.get("filename") or ""))[0] or "directory"
    media = (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        if format == "xlsx"
        else "text/csv"
    )
    return FileResponse(path, media_type=media, filename=f"{stem}_enriched.{format}")


@app.delete("/jobs/{job_id}")
def job_delete(job_id: str):
    job = JOBS.pop(job_id, None)
    if not job:
        raise HTTPException(404, "unknown job")
    # May be a job we inherited from a previous process, so kill by pid if we
    # don't hold the handle -- otherwise the worker keeps running (and keeps a
    # Chromium alive) with nothing tracking it.
    _stop_worker(job)
    shutil.rmtree(_job_dir(job_id), ignore_errors=True)
    return {"ok": True}
