from __future__ import annotations

import logging
from html import escape

import stripe
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from .config import settings
from .jobs import JobState, job_manager

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/billing", tags=["billing"])

stripe.api_key = settings.stripe_secret_key


def create_checkout_session(job: JobState):
    return stripe.checkout.Session.create(
        mode="payment",
        line_items=[
            {
                "price_data": {
                    "currency": settings.stripe_currency,
                    "unit_amount": job.price_cents,
                    "product_data": {"name": f"Translation: {job.source_filename}"},
                },
                "quantity": 1,
            }
        ],
        metadata={"job_id": job.id},
        success_url=f"{settings.public_base_url}/billing/success?job_id={job.id}",
        cancel_url=f"{settings.public_base_url}/billing/cancel?job_id={job.id}",
    )


@router.get("/success", response_class=HTMLResponse)
async def billing_success(job_id: str) -> str:
    safe_job_id = escape(job_id)
    return (
        "<html><body><h1>Payment received</h1>"
        f"<p>Your translation job <code>{safe_job_id}</code> will start shortly.</p>"
        f'<p><a href="/translations/{safe_job_id}">Check status</a></p>'
        "</body></html>"
    )


@router.get("/cancel", response_class=HTMLResponse)
async def billing_cancel(job_id: str) -> str:
    safe_job_id = escape(job_id)
    return (
        "<html><body><h1>Payment cancelled</h1>"
        f"<p>Your translation job <code>{safe_job_id}</code> was not started.</p>"
        f'<p><a href="/translations/{safe_job_id}">Check status</a></p>'
        "</body></html>"
    )


@router.post("/webhook")
async def stripe_webhook(request: Request) -> dict:
    # Read the raw bytes (not FastAPI's parsed JSON) — Stripe signs the exact body.
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature", "")
    try:
        event = stripe.Webhook.construct_event(payload, sig_header, settings.stripe_webhook_secret)
    except (ValueError, stripe.SignatureVerificationError):
        raise HTTPException(400, "Invalid webhook payload or signature")

    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]
        try:
            job_id = session["metadata"]["job_id"]
        except (KeyError, TypeError):
            job_id = None
        job = job_manager.get(job_id) if job_id else None

        if job is None:
            # Never 500 here: Stripe retries indefinitely on failure, and a
            # missing job (e.g. deleted, or a stale/foreign event) is not
            # something retrying will fix.
            logger.warning("Stripe webhook: job %s not found, ignoring", job_id)
            return {"status": "ignored"}

        if job.stripe_payment_status == "paid" or job.status != "awaiting_payment":
            # Covers Stripe redelivery and jobs cancelled before payment landed.
            return {"status": "ignored"}

        job_manager.mark_paid(job)
        job_manager.submit(
            job,
            target_language=job.target_language,
            concurrency=job.concurrency,
            user_prompt=job.user_prompt,
            submit_kind=job.submit_kind,
        )

    return {"status": "ok"}
