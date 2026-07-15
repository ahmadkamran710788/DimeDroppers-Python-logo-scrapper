"""FastAPI job service: upload a directory sheet -> enriched xlsx/csv.

Job model is ported from DD-Scrapper/api.py: an in-memory dict holding live Popen
handles, status derived by polling the OS process. Consequences carried over
deliberately -- JOBS is process-local, so run exactly ONE uvicorn worker, and jobs
do not survive a restart.
"""

import json
import os
import shutil
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
JOBS_DIR = os.path.join(HERE, "jobs")

MAX_CONCURRENT_JOBS = int(os.environ.get("MAX_CONCURRENT_JOBS", "1"))
JOB_MAX_RUNTIME_SECONDS = int(os.environ.get("JOB_MAX_RUNTIME_SECONDS", "3600"))
MAX_UPLOAD_BYTES = int(os.environ.get("MAX_UPLOAD_BYTES", str(10 * 1024 * 1024)))
ALLOWED_EXT = (".xlsx", ".xlsm", ".csv")

# job_id -> {status, started_at, error, filename, row_count, proc}
JOBS = {}


@asynccontextmanager
async def lifespan(_app):
    # Jobs cannot outlive the process that tracked them, so stale output is noise.
    shutil.rmtree(JOBS_DIR, ignore_errors=True)
    os.makedirs(JOBS_DIR, exist_ok=True)
    yield


app = FastAPI(title="DD Logo Scraper", lifespan=lifespan)

_origin = os.environ.get("FRONTEND_ORIGIN", "*")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"] if _origin == "*" else [o.strip() for o in _origin.split(",")],
    allow_methods=["*"],
    allow_headers=["*"],
)


def _job_dir(job_id):
    return os.path.join(JOBS_DIR, job_id)


def _refresh(job_id):
    job = JOBS.get(job_id)
    if not job or job["status"] != "running":
        return job
    proc = job.get("proc")
    if proc is None:
        return job

    rc = proc.poll()
    if rc is None:
        if time.time() - job["started_at"] > JOB_MAX_RUNTIME_SECONDS:
            proc.terminate()
            job["status"] = "error"
            job["error"] = "job exceeded the maximum runtime and was stopped"
        return job

    if rc == 0:
        job["status"] = "done"
    else:
        job["status"] = "error"
        err_path = os.path.join(_job_dir(job_id), "error.txt")
        if os.path.exists(err_path):
            with open(err_path, encoding="utf-8") as fh:
                job["error"] = fh.read().strip().splitlines()[0][:400]
        else:
            job["error"] = f"worker exited with code {rc}"
    return job


def _refresh_all():
    # Run before the concurrency check so finished-but-unpolled jobs free their slot.
    for jid in list(JOBS):
        _refresh(jid)


def _read_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return default


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

    proc = subprocess.Popen([sys.executable, "worker.py", out_dir], cwd=HERE)
    JOBS[job_id] = {
        "status": "running",
        "started_at": time.time(),
        "error": None,
        "filename": file.filename,
        "row_count": len(meta["rows"]),
        "proc": proc,
    }
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
    proc = job.get("proc")
    if proc and proc.poll() is None:
        proc.terminate()
    shutil.rmtree(_job_dir(job_id), ignore_errors=True)
    return {"ok": True}
