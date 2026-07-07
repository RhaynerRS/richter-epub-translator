from __future__ import annotations

import asyncio
import json
import shutil
from enum import Enum
from typing import Optional

from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse

from .auth import get_current_user
from .auth import router as auth_router
from .config import settings
from .db import User
from .jobs import TERMINAL_STATUSES, JobState, job_manager
from .payments import create_checkout_session
from .payments import router as billing_router
from .pricing import estimate_billable_tokens, estimate_price_cents

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

app.include_router(auth_router)
app.include_router(billing_router)


@app.on_event("startup")
async def _bind_job_manager_loop() -> None:
    job_manager.bind_loop(asyncio.get_running_loop())


def _check_job_access(job: JobState, current_user: Optional[User]) -> None:
    """404 (not 403) when the job belongs to someone else, so we don't reveal
    that a job with this id exists."""
    allowed_owner_id = current_user.id if current_user else None
    if job.owner_user_id not in (None, allowed_owner_id):
        raise HTTPException(404, "Job not found")


@app.post("/translations", status_code=202)
async def create_translation(
    file: UploadFile = File(...),
    target_language: str = Form(...),
    concurrency: int = Form(1),
    user_prompt: Optional[str] = Form(None),
    submit_kind: SubmitKindParam = Form(SubmitKindParam.APPEND_BLOCK),
    current_user: Optional[User] = Depends(get_current_user),
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
        owner_user_id=current_user.id if current_user else None,
        user_prompt=user_prompt,
    )

    with job.source_path.open("wb") as out_file:
        shutil.copyfileobj(file.file, out_file)

    if settings.payment_enabled:
        estimated_tokens = estimate_billable_tokens(job.source_path)
        price_cents = estimate_price_cents(estimated_tokens)
        job_manager.mark_awaiting_payment(
            job,
            estimated_tokens=estimated_tokens,
            price_cents=price_cents,
            currency=settings.stripe_currency,
        )
        checkout_session = create_checkout_session(job)
        job_manager.set_stripe_checkout_session(job, checkout_session.id)
        return {"job_id": job.id, "status": job.status, "checkout_url": checkout_session.url}

    job_manager.submit(
        job,
        target_language=target_language,
        concurrency=concurrency,
        user_prompt=user_prompt,
        submit_kind=submit_kind.value,
    )

    return {"job_id": job.id, "status": job.status}


@app.get("/translations")
async def list_translations(current_user: Optional[User] = Depends(get_current_user)):
    jobs = job_manager.list()
    if current_user is not None:
        jobs = [job for job in jobs if job.owner_user_id == current_user.id]
    return [job.to_public_dict() for job in jobs]


@app.get("/translations/{job_id}")
async def get_translation(job_id: str, current_user: Optional[User] = Depends(get_current_user)):
    job = job_manager.get(job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    _check_job_access(job, current_user)
    return job.to_public_dict()


@app.post("/translations/{job_id}/cancel")
async def cancel_translation(job_id: str, current_user: Optional[User] = Depends(get_current_user)):
    job = job_manager.get(job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    _check_job_access(job, current_user)
    result = job_manager.cancel(job)
    if result is None:
        raise HTTPException(409, f"Job cannot be cancelled (status={job.status})")
    return job.to_public_dict()


@app.get("/translations/{job_id}/events")
async def stream_translation_events(job_id: str, current_user: Optional[User] = Depends(get_current_user)):
    job = job_manager.get(job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    _check_job_access(job, current_user)

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
async def download_translation(job_id: str, current_user: Optional[User] = Depends(get_current_user)):
    job = job_manager.get(job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    _check_job_access(job, current_user)
    if job.status != "completed":
        raise HTTPException(409, f"Job is not completed (status={job.status})")
    return FileResponse(
        path=job.target_path,
        media_type="application/epub+zip",
        filename=f"translated_{job.source_filename}",
    )
