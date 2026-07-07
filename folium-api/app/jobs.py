from __future__ import annotations

import asyncio
import threading
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from epub_translator import LLM, SubmitKind, translate

from .config import settings
from .db import JobStore

CANCELLABLE_STATUSES = ("queued", "awaiting_payment", "running", "cancelling")
TERMINAL_STATUSES = ("completed", "failed", "cancelled")


class _JobCancelled(Exception):
    pass


@dataclass
class JobState:
    id: str
    source_filename: str
    target_language: str = ""
    submit_kind: str = ""
    concurrency: int = 1
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    status: str = "queued"  # queued | awaiting_payment | running | cancelling | completed | failed | cancelled
    progress: float = 0.0
    error: Optional[str] = None
    last_warning: Optional[str] = None
    source_path: Optional[Path] = None
    target_path: Optional[Path] = None
    owner_user_id: Optional[str] = None
    user_prompt: Optional[str] = None
    estimated_tokens: Optional[int] = None
    price_cents: Optional[int] = None
    currency: Optional[str] = None
    stripe_checkout_session_id: Optional[str] = None
    stripe_payment_status: Optional[str] = None
    future: Optional[Future] = field(default=None, repr=False)
    cancel_event: threading.Event = field(default_factory=threading.Event, repr=False)
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    subscribers: list["asyncio.Queue[dict]"] = field(default_factory=list, repr=False)

    def to_public_dict(self) -> dict:
        with self.lock:
            return {
                "job_id": self.id,
                "source_filename": self.source_filename,
                "target_language": self.target_language,
                "submit_kind": self.submit_kind,
                "concurrency": self.concurrency,
                "created_at": self.created_at.isoformat(),
                "status": self.status,
                "progress": round(self.progress, 4),
                "error": self.error,
                "warning": self.last_warning,
                "owner_user_id": self.owner_user_id,
                "user_prompt": self.user_prompt,
                "estimated_tokens": self.estimated_tokens,
                "price_cents": self.price_cents,
                "currency": self.currency,
                "stripe_payment_status": self.stripe_payment_status,
            }


class JobManager:
    def __init__(self, max_workers: int, storage_dir: Path):
        self.upload_dir = storage_dir / "uploads"
        self.output_dir = storage_dir / "outputs"
        self.upload_dir.mkdir(parents=True, exist_ok=True)
        self.output_dir.mkdir(parents=True, exist_ok=True)

        self._jobs: dict[str, JobState] = {}
        self._executor = ThreadPoolExecutor(max_workers=max_workers)
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._store = JobStore(storage_dir / "jobs.db")
        self._load_persisted_jobs()

    def _load_persisted_jobs(self) -> None:
        """Restore jobs recorded before the last restart. Any job that was still
        in-flight lost its background thread when the process exited, so it's
        marked failed instead of being silently stuck as "running" forever."""
        for row in self._store.load_all():
            status = row["status"]
            error = row["error"]
            if status not in TERMINAL_STATUSES:
                status = "failed"
                error = error or "Interrupted by server restart"

            job = JobState(
                id=row["id"],
                source_filename=row["source_filename"],
                target_language=row["target_language"],
                submit_kind=row["submit_kind"],
                concurrency=row["concurrency"],
                created_at=datetime.fromisoformat(row["created_at"]),
                status=status,
                progress=row["progress"],
                error=error,
                last_warning=row["warning"],
                source_path=Path(row["source_path"]) if row["source_path"] else None,
                target_path=Path(row["target_path"]) if row["target_path"] else None,
                owner_user_id=row["owner_user_id"],
                user_prompt=row["user_prompt"],
                estimated_tokens=row["estimated_tokens"],
                price_cents=row["price_cents"],
                currency=row["currency"],
                stripe_checkout_session_id=row["stripe_checkout_session_id"],
                stripe_payment_status=row["stripe_payment_status"],
            )
            self._jobs[job.id] = job
            if status != row["status"]:
                self._persist(job)

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Bind the running FastAPI event loop so worker threads can publish
        job updates to SSE subscribers via `call_soon_threadsafe`."""
        self._loop = loop

    def create_job(
        self,
        source_filename: str,
        target_language: str,
        submit_kind: str,
        concurrency: int,
        owner_user_id: Optional[str] = None,
        user_prompt: Optional[str] = None,
    ) -> JobState:
        job_id = uuid.uuid4().hex
        job = JobState(
            id=job_id,
            source_filename=source_filename,
            target_language=target_language,
            submit_kind=submit_kind,
            concurrency=concurrency,
            source_path=self.upload_dir / f"{job_id}.epub",
            target_path=self.output_dir / f"{job_id}.epub",
            owner_user_id=owner_user_id,
            user_prompt=user_prompt,
        )
        self._jobs[job.id] = job
        self._persist(job)
        return job

    def get(self, job_id: str) -> Optional[JobState]:
        return self._jobs.get(job_id)

    def list(self) -> list[JobState]:
        return list(self._jobs.values())

    def subscribe(self, job: JobState) -> "asyncio.Queue[dict]":
        queue: "asyncio.Queue[dict]" = asyncio.Queue()
        job.subscribers.append(queue)
        return queue

    def unsubscribe(self, job: JobState, queue: "asyncio.Queue[dict]") -> None:
        if queue in job.subscribers:
            job.subscribers.remove(queue)

    def _publish(self, job: JobState) -> None:
        """Called from worker threads; persists the current job state and hands
        it off to the event loop so it can be delivered to any subscribed SSE
        streams."""
        self._persist(job)
        if self._loop is None:
            return
        self._loop.call_soon_threadsafe(self._deliver, job)

    def _persist(self, job: JobState) -> None:
        with job.lock:
            row = {
                "id": job.id,
                "source_filename": job.source_filename,
                "target_language": job.target_language,
                "submit_kind": job.submit_kind,
                "concurrency": job.concurrency,
                "created_at": job.created_at.isoformat(),
                "status": job.status,
                "progress": job.progress,
                "error": job.error,
                "warning": job.last_warning,
                "source_path": str(job.source_path) if job.source_path else None,
                "target_path": str(job.target_path) if job.target_path else None,
                "owner_user_id": job.owner_user_id,
                "user_prompt": job.user_prompt,
                "estimated_tokens": job.estimated_tokens,
                "price_cents": job.price_cents,
                "currency": job.currency,
                "stripe_checkout_session_id": job.stripe_checkout_session_id,
                "stripe_payment_status": job.stripe_payment_status,
            }
        self._store.upsert(row)

    @staticmethod
    def _deliver(job: JobState) -> None:
        data = job.to_public_dict()
        for queue in job.subscribers:
            queue.put_nowait(data)

    def submit(
        self,
        job: JobState,
        target_language: str,
        concurrency: int,
        user_prompt: Optional[str],
        submit_kind: str,
    ) -> None:
        job.future = self._executor.submit(
            self._run, job, target_language, concurrency, user_prompt, submit_kind
        )

    def mark_awaiting_payment(
        self,
        job: JobState,
        estimated_tokens: int,
        price_cents: int,
        currency: str,
    ) -> None:
        with job.lock:
            job.status = "awaiting_payment"
            job.estimated_tokens = estimated_tokens
            job.price_cents = price_cents
            job.currency = currency
        self._publish(job)

    def set_stripe_checkout_session(self, job: JobState, stripe_checkout_session_id: str) -> None:
        with job.lock:
            job.stripe_checkout_session_id = stripe_checkout_session_id
        self._publish(job)

    def mark_paid(self, job: JobState) -> None:
        with job.lock:
            job.stripe_payment_status = "paid"
        self._publish(job)

    def cancel(self, job: JobState) -> Optional[str]:
        """Request cancellation of a job and discard any work done so far.

        Returns the resulting status, or None if the job had already finished
        and can no longer be cancelled.
        """
        with job.lock:
            if job.status not in CANCELLABLE_STATUSES:
                return None
            job.cancel_event.set()
            status_before_cancel = job.status

        if status_before_cancel == "awaiting_payment":
            # Never reached the executor (no future was ever created), so
            # there's nothing to interrupt: it can be cancelled outright.
            stopped_before_start = True
        elif status_before_cancel == "queued":
            stopped_before_start = job.future is not None and job.future.cancel()
        else:
            stopped_before_start = False

        with job.lock:
            if stopped_before_start:
                job.status = "cancelled"
            elif job.status not in TERMINAL_STATUSES:
                job.status = "cancelling"
            result_status = job.status

        if stopped_before_start:
            self._discard_job_files(job)

        self._publish(job)
        return result_status

    @staticmethod
    def _discard_job_files(job: JobState) -> None:
        for path in (job.target_path, job.source_path):
            if path is not None:
                path.unlink(missing_ok=True)

    def _run(
        self,
        job: JobState,
        target_language: str,
        concurrency: int,
        user_prompt: Optional[str],
        submit_kind: str,
    ) -> None:
        with job.lock:
            job.status = "running"
        self._publish(job)

        def on_progress(progress: float) -> None:
            if job.cancel_event.is_set():
                raise _JobCancelled()
            with job.lock:
                job.progress = progress
            self._publish(job)

        def on_fill_failed(event) -> None:
            with job.lock:
                job.last_warning = (
                    f"{event.error_message} "
                    f"(retry {event.retried_count}, "
                    f"over_maximum_retries={event.over_maximum_retries})"
                )
            self._publish(job)

        try:
            # Dual-LLM setup matching the official books-translator-ng block:
            # translation favors fluency with a higher temperature/top_p,
            # while fill favors literal structure preservation with a low,
            # retry-escalating temperature. llm_extra_body carries provider-
            # specific options (e.g. Ollama's num_ctx) and is None for
            # providers like DeepSeek that don't need it.
            translation_llm = LLM(
                key=settings.llm_api_key,
                url=settings.llm_base_url,
                model=settings.llm_model,
                token_encoding=settings.token_encoding,
                timeout=360.0,
                retry_times=10,
                retry_interval_seconds=0.75,
                temperature=0.8,
                top_p=0.6,
                extra_body=settings.llm_extra_body,
            )
            fill_llm = LLM(
                key=settings.llm_api_key,
                url=settings.llm_base_url,
                model=settings.llm_model,
                token_encoding=settings.token_encoding,
                timeout=360.0,
                retry_times=10,
                retry_interval_seconds=0.75,
                temperature=(0.2, 0.9),
                top_p=(0.9, 1.0),
                extra_body=settings.llm_extra_body,
            )
            translate(
                source_path=job.source_path,
                target_path=job.target_path,
                target_language=target_language,
                submit=SubmitKind[submit_kind],
                max_group_tokens=settings.max_group_tokens,
                user_prompt=user_prompt,
                concurrency=concurrency,
                translation_llm=translation_llm,
                fill_llm=fill_llm,
                on_progress=on_progress,
                on_fill_failed=on_fill_failed,
            )
            with job.lock:
                if job.status == "cancelling":
                    job.status = "cancelled"
                else:
                    job.progress = 1.0
                    job.status = "completed"
            if job.status == "cancelled":
                self._discard_job_files(job)
        except _JobCancelled:
            with job.lock:
                job.status = "cancelled"
            self._discard_job_files(job)
        except Exception as exc:
            with job.lock:
                job.status = "failed"
                job.error = str(exc)
        finally:
            self._publish(job)


job_manager = JobManager(max_workers=settings.job_workers, storage_dir=Path(settings.storage_dir))
