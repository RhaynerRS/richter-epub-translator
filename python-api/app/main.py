from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse

from .config import settings
from .jobs import job_manager

app = FastAPI(title="Lexicast API")

STORAGE_DIR = Path(settings.storage_dir)
UPLOAD_DIR = STORAGE_DIR / "uploads"
OUTPUT_DIR = STORAGE_DIR / "outputs"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


@app.post("/translations", status_code=202)
async def create_translation(
    file: UploadFile = File(...),
    target_language: str = Form(...),
    concurrency: int = Form(1),
    user_prompt: Optional[str] = Form(None),
):
    if not file.filename or not file.filename.lower().endswith(".epub"):
        raise HTTPException(400, "File must be an .epub")
    if concurrency < 1:
        raise HTTPException(400, "concurrency must be >= 1")

    job = job_manager.create_job(source_filename=file.filename)
    job.source_path = UPLOAD_DIR / f"{job.id}.epub"
    job.target_path = OUTPUT_DIR / f"{job.id}.epub"

    with job.source_path.open("wb") as out_file:
        shutil.copyfileobj(file.file, out_file)

    job_manager.submit(
        job,
        target_language=target_language,
        concurrency=concurrency,
        user_prompt=user_prompt,
    )

    return {"job_id": job.id, "status": job.status}


@app.get("/translations/{job_id}")
async def get_translation(job_id: str):
    job = job_manager.get(job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    return job.to_public_dict()


@app.get("/translations/{job_id}/events")
async def stream_translation_events(job_id: str):
    job = job_manager.get(job_id)
    if job is None:
        raise HTTPException(404, "Job not found")

    async def event_stream():
        while True:
            data = job.to_public_dict()
            yield f"data: {json.dumps(data)}\n\n"
            if data["status"] in ("completed", "failed"):
                break
            await asyncio.sleep(1)

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@app.get("/translations/{job_id}/download")
async def download_translation(job_id: str):
    job = job_manager.get(job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    if job.status != "completed":
        raise HTTPException(409, f"Job is not completed (status={job.status})")
    return FileResponse(
        path=job.target_path,
        media_type="application/epub+zip",
        filename=f"translated_{job.source_filename}",
    )
