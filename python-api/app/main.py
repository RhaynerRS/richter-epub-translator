from __future__ import annotations

import asyncio
import json
import shutil
from enum import Enum
from typing import Optional

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse

from .config import settings
from .jobs import TERMINAL_STATUSES, job_manager

SSE_HEARTBEAT_SECONDS = 15


class SubmitKindParam(str, Enum):
    REPLACE = "REPLACE"
    APPEND_TEXT = "APPEND_TEXT"
    APPEND_BLOCK = "APPEND_BLOCK"


app = FastAPI(title="Lexicast API")

if settings.frontend_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.frontend_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )


@app.on_event("startup")
async def _bind_job_manager_loop() -> None:
    job_manager.bind_loop(asyncio.get_running_loop())


@app.post("/translations", status_code=202)
async def create_translation(
    file: UploadFile = File(...),
    target_language: str = Form(...),
    concurrency: int = Form(1),
    user_prompt: Optional[str] = Form(None),
    submit_kind: SubmitKindParam = Form(SubmitKindParam.APPEND_BLOCK),
):
    if not file.filename or not file.filename.lower().endswith(".epub"):
        raise HTTPException(400, "File must be an .epub")
    if concurrency < 1:
        raise HTTPException(400, "concurrency must be >= 1")

    job = job_manager.create_job(
        source_filename=file.filename,
        target_language=target_language,
        submit_kind=submit_kind.value,
        concurrency=concurrency,
    )

    with job.source_path.open("wb") as out_file:
        shutil.copyfileobj(file.file, out_file)

    job_manager.submit(
        job,
        target_language=target_language,
        concurrency=concurrency,
        user_prompt=user_prompt,
        submit_kind=submit_kind.value,
    )

    return {"job_id": job.id, "status": job.status}


@app.get("/translations")
async def list_translations():
    return [job.to_public_dict() for job in job_manager.list()]


@app.get("/translations/{job_id}")
async def get_translation(job_id: str):
    job = job_manager.get(job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    return job.to_public_dict()


@app.post("/translations/{job_id}/cancel")
async def cancel_translation(job_id: str):
    job = job_manager.get(job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    result = job_manager.cancel(job)
    if result is None:
        raise HTTPException(409, f"Job cannot be cancelled (status={job.status})")
    return job.to_public_dict()


@app.get("/translations/{job_id}/events")
async def stream_translation_events(job_id: str):
    job = job_manager.get(job_id)
    if job is None:
        raise HTTPException(404, "Job not found")

    queue = job_manager.subscribe(job)

    async def event_stream():
        try:
            data = job.to_public_dict()
            yield f"data: {json.dumps(data)}\n\n"
            if data["status"] in TERMINAL_STATUSES:
                return

            while True:
                try:
                    data = await asyncio.wait_for(queue.get(), timeout=SSE_HEARTBEAT_SECONDS)
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
                    continue

                yield f"data: {json.dumps(data)}\n\n"
                if data["status"] in TERMINAL_STATUSES:
                    break
        finally:
            job_manager.unsubscribe(job, queue)

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
