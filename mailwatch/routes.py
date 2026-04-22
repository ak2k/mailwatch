"""HTTP + WebSocket routes for mailwatch.

All routes are wired onto a single :class:`~fastapi.APIRouter` so the app
factory in :mod:`mailwatch.app` can mount everything with one
``include_router`` call. State (DB connection, shared DB lock, USPS
clients, settings, templates) is pulled off ``request.app.state`` via
small ``Depends`` helpers — no module globals.

Session cookies carry UI-only state (sender text, recipient address,
serial last generated). Nothing sensitive goes in there; OAuth tokens
live in SQLite ``app_state``.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import sqlite3
from pathlib import Path
from time import time as _now
from typing import Annotated, Any, Literal
from urllib.parse import urlencode

from fastapi import (
    APIRouter,
    Depends,
    Form,
    HTTPException,
    Request,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field, ValidationError

from mailwatch import db, imb, pdf
from mailwatch.avery import AVERY, DEFAULT_AVERY, DISPLAY_NAMES as _AVERY_LABELS
from mailwatch.config import Settings
from mailwatch.layouts import DEFAULT_ENVELOPE, DISPLAY_NAMES as _ENVELOPE_LABELS, ENVELOPES
from mailwatch.models import AddressRequest, PushFeedPayload
from mailwatch.usps_api import IVMTRClient, NewApiClient

logger = logging.getLogger(__name__)

# Jinja2 template loader — the templates live alongside the package.
_TEMPLATE_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATE_DIR))

router = APIRouter()

# --------------------------------------------------------------------------- #
# Dependency helpers                                                          #
# --------------------------------------------------------------------------- #


def get_settings_dep(request: Request) -> Settings:
    """Return the :class:`Settings` bound to the app state."""
    settings: Settings = request.app.state.settings
    return settings


def get_db_conn(request: Request) -> sqlite3.Connection:
    """Return the shared :class:`sqlite3.Connection`."""
    conn: sqlite3.Connection = request.app.state.db
    return conn


def get_db_lock(request: Request) -> asyncio.Lock:
    """Return the app-wide DB lock.

    Any handler that dispatches to :func:`asyncio.to_thread(db.*)` MUST
    acquire this lock first. ``sqlite3.Connection`` is not re-entrant
    across threads even with ``check_same_thread=False`` — see the note in
    :func:`mailwatch.usps_api._run_db`.
    """
    lock: asyncio.Lock = request.app.state.db_lock
    return lock


def get_db_locked(
    conn: sqlite3.Connection = Depends(get_db_conn),
    lock: asyncio.Lock = Depends(get_db_lock),
) -> tuple[sqlite3.Connection, asyncio.Lock]:
    """Return ``(conn, lock)`` for handlers that need both."""
    return conn, lock


def get_new_api(request: Request) -> NewApiClient:
    """Return the shared :class:`NewApiClient`."""
    client: NewApiClient = request.app.state.new_api
    return client


async def _db_call(lock: asyncio.Lock, fn: Any, *args: Any) -> Any:
    """Run a blocking ``db.*`` call on a worker thread under the shared lock."""
    async with lock:
        return await asyncio.to_thread(fn, *args)


# --------------------------------------------------------------------------- #
# Form + WS input models                                                      #
# --------------------------------------------------------------------------- #


_ZIP_PATTERN = r"^\d{5}(?:-?\d{4})?$"


class GenerateForm(BaseModel):
    """Validated POST body for :func:`post_generate`.

    Accepts the flat form fields the ``index.html`` template submits. ZIP
    format (``12345`` or ``12345-6789``) is enforced here so the handler
    can assume clean input.

    Output options (envelope size, Avery part, row/col, human-readable,
    etc.) are NOT on this form — they live on the preview page as query
    parameters, so changing them regenerates the PDF without touching
    the session or the allocated IMb serial.
    """

    sender_address: str = Field(..., min_length=1)
    recipient_name: str = Field(..., min_length=1)
    recipient_company: str | None = None
    recipient_street: str = Field(..., min_length=1)
    recipient_address2: str | None = None
    recipient_city: str = Field(..., min_length=1)
    recipient_state: str = Field(..., min_length=2, max_length=2, pattern=r"^[A-Za-z]{2}$")
    recipient_zip: str = Field(..., pattern=_ZIP_PATTERN)
    format_type: Literal["envelope", "avery"] = "envelope"


class TrackWSRequest(BaseModel):
    """WebSocket payload for the tracking stream."""

    serial: int = Field(..., ge=0)
    receipt_zip: str = Field(..., pattern=_ZIP_PATTERN)


# --------------------------------------------------------------------------- #
# Pure helpers                                                                #
# --------------------------------------------------------------------------- #


def _clean_zip(raw: str) -> str:
    """Strip ZIP hyphen / whitespace; return digits only."""
    return "".join(ch for ch in raw if ch.isdigit())


def _day_bucket(epoch_seconds: float | None = None) -> int:
    """Return the UTC-day integer used as the ``serial_counters`` PK."""
    ts = epoch_seconds if epoch_seconds is not None else _now()
    return int(ts // 86400)


def _build_tracking(settings: Settings, serial: int) -> str:
    """Assemble the 20-digit IMb tracking string.

    USPS splits the Mailer ID + Serial field 6+9 (MID starts 0-8) or 9+6
    (MID starts with 9). We mirror the same branch used in
    :func:`mailwatch.imb.encode`.
    """
    mid_str = str(settings.MAILER_ID)
    if mid_str.startswith("9"):
        return (
            f"{settings.BARCODE_ID:02d}"
            f"{settings.SRV_TYPE:03d}"
            f"{settings.MAILER_ID:09d}"
            f"{serial:06d}"
        )
    return (
        f"{settings.BARCODE_ID:02d}"
        f"{settings.SRV_TYPE:03d}"
        f"{settings.MAILER_ID:06d}"
        f"{serial:09d}"
    )


def _recipient_lines_from_session(recipient: dict[str, Any]) -> list[str]:
    """Render a recipient dict (as stored in the session) as display lines."""
    lines: list[str] = [recipient["name"]]
    if recipient.get("company"):
        lines.append(recipient["company"])
    lines.append(recipient["street"])
    if recipient.get("address2"):
        lines.append(recipient["address2"])
    lines.append(f"{recipient['city']}, {recipient['state']} {recipient['zip']}")
    return lines


def _sender_lines(sender_address: str) -> list[str]:
    """Split the sender textarea into non-empty lines."""
    return [line.strip() for line in sender_address.splitlines() if line.strip()]


# --------------------------------------------------------------------------- #
# Routes                                                                      #
# --------------------------------------------------------------------------- #


@router.get("/", response_class=HTMLResponse)
async def get_index(request: Request) -> Response:
    """Render the data-entry form.

    Pre-fills sender + recipient from session when present so the "Edit
    recipient (new serial)" link on the preview page can bounce back here
    without losing context. Output-format options (size, part, mode, etc.)
    do NOT live here — they're on the preview page.
    """
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "sender_address": request.session.get("sender_address", ""),
            "recipient": request.session.get("recipient"),
        },
    )


@router.post("/generate")
async def post_generate(
    request: Request,
    sender_address: Annotated[str, Form()],
    recipient_name: Annotated[str, Form()],
    recipient_street: Annotated[str, Form()],
    recipient_city: Annotated[str, Form()],
    recipient_state: Annotated[str, Form()],
    recipient_zip: Annotated[str, Form()],
    recipient_company: Annotated[str | None, Form()] = None,
    recipient_address2: Annotated[str | None, Form()] = None,
    format_type: Annotated[Literal["envelope", "avery"], Form()] = "envelope",
    settings: Settings = Depends(get_settings_dep),
    locked: tuple[sqlite3.Connection, asyncio.Lock] = Depends(get_db_locked),
) -> Response:
    """Allocate a serial, store mail piece in session, redirect to ``/preview``.

    All output options (envelope size, Avery part, mode, row/col,
    human-readable) are stateless query parameters on ``/preview`` + the
    PDF download URL. Nothing about them is persisted to the session,
    so the user can regen by clicking radios without ever re-hitting
    ``/generate`` — the serial stays stable.
    """
    try:
        form = GenerateForm(
            sender_address=sender_address,
            recipient_name=recipient_name,
            recipient_company=recipient_company,
            recipient_street=recipient_street,
            recipient_address2=recipient_address2,
            recipient_city=recipient_city,
            recipient_state=recipient_state.upper(),
            recipient_zip=recipient_zip,
            format_type=format_type,
        )
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.errors()) from exc

    conn, lock = locked
    bucket = _day_bucket()
    serial: int = await _db_call(lock, db.next_serial, conn, bucket)

    tracking = _build_tracking(settings, serial)
    routing = _clean_zip(form.recipient_zip)
    # Validate encoding end-to-end — raises ValueError on any out-of-range field.
    imb.encode(
        settings.BARCODE_ID,
        settings.SRV_TYPE,
        settings.MAILER_ID,
        serial,
        routing,
    )

    # Session holds the piece-of-mail identity (sender/recipient/serial).
    # Output-format preferences ride on the URL, not the session.
    request.session["sender_address"] = form.sender_address
    request.session["recipient"] = {
        "name": form.recipient_name,
        "company": form.recipient_company,
        "street": form.recipient_street,
        "address2": form.recipient_address2,
        "city": form.recipient_city,
        "state": form.recipient_state,
        "zip": form.recipient_zip,
    }
    request.session["serial"] = serial
    request.session["tracking"] = tracking
    request.session["routing"] = routing

    # 303 = "see other" — turns the POST into a GET for the redirect target,
    # so refreshing the preview page doesn't resubmit the form.
    return RedirectResponse(
        url=f"/preview?fmt={form.format_type}",
        status_code=303,
    )


# --------------------------------------------------------------------------- #
# Preview + PDF download                                                      #
# --------------------------------------------------------------------------- #

# Session model:
#  - FastAPI SessionMiddleware, cookie-backed, HttpOnly + SameSite=Lax
#    (Secure in production via create_app(session_https_only=True)).
#  - Holds: {sender_address, recipient, serial, tracking, routing}.
#  - Lifetime: browser-close = gone (no server-side store).
#  - Single-user service: no inter-tab lock. Two tabs = two cookies =
#    two independent serials, which is the intuitive behavior.
#  - Serial stability: allocated once at /generate, preserved across
#    every /preview option change. Only "Edit recipient" → /generate
#    allocates a fresh serial.


def _require_session_piece(request: Request) -> dict[str, Any]:
    """Extract the session-held mail piece or raise 400."""
    recipient = request.session.get("recipient")
    serial = request.session.get("serial")
    tracking = request.session.get("tracking")
    routing = request.session.get("routing")
    sender_address = request.session.get("sender_address")
    if not (recipient and serial is not None and tracking and sender_address):
        raise HTTPException(
            status_code=400,
            detail="No envelope in session; generate one first.",
        )
    return {
        "sender_address": sender_address,
        "recipient": recipient,
        "serial": serial,
        "tracking": tracking,
        "routing": routing or "",
    }


def _parse_hr(value: str | None) -> bool:
    """Query-string bool: truthy if '1'/'true'/'on'/'yes' (case-insensitive)."""
    if value is None:
        return True
    return value.strip().lower() in {"1", "true", "on", "yes"}


@router.get("/preview", response_class=HTMLResponse)
async def get_preview(
    request: Request,
    fmt: Literal["envelope", "avery"] = "envelope",
    size: str = DEFAULT_ENVELOPE,
    part: str = DEFAULT_AVERY,
    mode: Literal["single", "fill"] = "fill",
    row: int = 1,
    col: int = 1,
    hr: str | None = None,
) -> Response:
    """Render the preview page with the current output options.

    All option state lives in the URL — changing a radio / select in the
    preview form navigates back to this route with different query params
    and the PDF embed reloads. No POST, no session mutation, no new
    serial.
    """
    piece = _require_session_piece(request)
    if size not in ENVELOPES:
        raise HTTPException(status_code=400, detail=f"unknown envelope size: {size}")
    if part not in AVERY:
        raise HTTPException(status_code=400, detail=f"unknown Avery part: {part}")

    tpl = AVERY[part]
    # Clamp row/col to the chosen Avery part's grid so a stale URL from a
    # bigger-grid part doesn't 500 on the downstream render.
    row = max(1, min(row, tpl.rows))
    col = max(1, min(col, tpl.cols))
    hr_on = _parse_hr(hr)

    # Envelope keys contain '#' / '/' which must be percent-encoded so the
    # browser doesn't treat them as fragment / path separators.
    qs = urlencode(
        {
            "fmt": fmt,
            "size": size,
            "part": part,
            "mode": mode,
            "row": row,
            "col": col,
            "hr": "1" if hr_on else "0",
        }
    )
    pdf_url = f"/download/pdf?{qs}"
    tracking_url = f"/tracking?serial={piece['serial']}&zip={piece['recipient']['zip']}"

    return templates.TemplateResponse(
        request,
        "preview.html",
        {
            "serial": piece["serial"],
            "recipient_zip": piece["recipient"]["zip"],
            "fmt": fmt,
            "size": size,
            "part": part,
            "mode": mode,
            "row": row,
            "col": col,
            "hr": hr_on,
            "envelope_choices": list(_ENVELOPE_LABELS.items()),
            "avery_choices": list(_AVERY_LABELS.items()),
            "avery_max_row": tpl.rows,
            "avery_max_col": tpl.cols,
            "pdf_url": pdf_url,
            "tracking_url": tracking_url,
        },
    )


@router.get("/download/pdf")
async def get_download_pdf(
    request: Request,
    fmt: Literal["envelope", "avery"] = "envelope",
    size: str = DEFAULT_ENVELOPE,
    part: str = DEFAULT_AVERY,
    mode: Literal["single", "fill"] = "fill",
    row: int = 1,
    col: int = 1,
    hr: str | None = None,
) -> Response:
    """Render the session's mail piece as a PDF using the given options."""
    piece = _require_session_piece(request)
    if size not in ENVELOPES:
        raise HTTPException(status_code=400, detail=f"unknown envelope size: {size}")
    if part not in AVERY:
        raise HTTPException(status_code=400, detail=f"unknown Avery part: {part}")

    sender_lines_val = _sender_lines(piece["sender_address"])
    recipient_lines_val = _recipient_lines_from_session(piece["recipient"])
    hr_on = _parse_hr(hr)

    buf = io.BytesIO()
    if fmt == "envelope":
        await asyncio.to_thread(
            pdf.render_envelope,
            sender_lines_val,
            recipient_lines_val,
            piece["tracking"],
            piece["routing"],
            buf,
            envelope_size=size,
            human_readable=hr_on,
        )
        filename = f"envelope-{piece['serial']}.pdf"
    else:
        tpl = AVERY[part]
        row = max(1, min(row, tpl.rows))
        col = max(1, min(col, tpl.cols))
        label: pdf.LabelData = {
            "recipient": recipient_lines_val,
            "tracking": piece["tracking"],
            "routing": piece["routing"],
        }
        await asyncio.to_thread(
            pdf.render_avery,
            label,
            buf,
            part=part,
            mode=mode,
            start_row=row,
            start_col=col,
            human_readable=hr_on,
        )
        filename = f"avery-{part}-{piece['serial']}.pdf"

    return Response(
        content=buf.getvalue(),
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{filename}"'},
    )


@router.post("/validate_address")
async def post_validate_address(
    payload: AddressRequest,
    client: NewApiClient = Depends(get_new_api),
) -> JSONResponse:
    """Standardize an address via the USPS NewApi client.

    Accepts an :class:`AddressRequest` JSON body and returns the
    standardized shape plus the ``full_zip`` computed field. Upstream
    errors surface as a 502 with the USPS message.
    """
    try:
        result = await client.validate_address(payload)
    except Exception as exc:  # noqa: BLE001 — USPS errors must surface as 502, not crash the handler
        logger.warning("address validation failed: %s", exc)
        return JSONResponse({"error": str(exc)}, status_code=502)

    return JSONResponse(
        {
            "standardized": {
                "firm": result.firm or (result.address.firm if result.address else None),
                "streetAddress": result.address.streetAddress,
                "secondaryAddress": result.address.secondaryAddress,
                "city": result.address.city,
                "state": result.address.state,
                "ZIPCode": result.address.ZIPCode,
                "ZIPPlus4": result.address.ZIPPlus4,
                "full_zip": result.full_zip,
            },
            "additionalInfo": (
                result.additionalInfo.model_dump(exclude_none=True)
                if result.additionalInfo
                else None
            ),
        }
    )


@router.get("/tracking", response_class=HTMLResponse)
async def get_tracking(
    request: Request,
    serial: str | None = None,
    zip: str | None = None,
) -> Response:
    """Render the tracking-form page with optional prefilled fields."""
    return templates.TemplateResponse(
        request,
        "tracking.html",
        {"serial": serial, "recipient_zip": zip},
    )


@router.websocket("/track-ws")
async def track_ws(websocket: WebSocket) -> None:
    """Stream tracking scans for a given serial + recipient ZIP.

    The client sends ``{"serial": int, "receipt_zip": "12345"}``; we
    build the IMb, query IV-MTR (live pull) + the local ``scan_events``
    table (webhook-fed cache), merge the two by event key, and reply
    with ``{"scans": [...]}``. Invalid input emits an ``{"error": ...}``
    message without dropping the connection.
    """
    await websocket.accept()
    app = websocket.app
    settings: Settings = app.state.settings
    conn: sqlite3.Connection = app.state.db
    lock: asyncio.Lock = app.state.db_lock
    ivmtr: IVMTRClient = app.state.ivmtr

    try:
        while True:
            try:
                raw = await websocket.receive_text()
            except WebSocketDisconnect:
                return

            try:
                parsed = TrackWSRequest.model_validate_json(raw)
            except ValidationError as exc:
                await websocket.send_json({"error": f"invalid request: {exc.errors()}"})
                continue
            except json.JSONDecodeError:
                await websocket.send_json({"error": "invalid JSON"})
                continue

            routing = _clean_zip(parsed.receipt_zip)
            tracking = _build_tracking(settings, parsed.serial)
            imb_key = f"{tracking}{routing}"

            merged: list[dict[str, Any]] = []

            try:
                live = await ivmtr.get_tracking(imb_key)
            except Exception as exc:  # noqa: BLE001 — live-pull failure falls back to stored events
                logger.info("IV-MTR live pull failed: %s", exc)
                live = None

            seen: set[str] = set()
            if live is not None and live.data is not None:
                for scan in live.data.scans:
                    key = f"{scan.scanDatetime.isoformat()}|{scan.scanEventCode}"
                    if key in seen:
                        continue
                    seen.add(key)
                    merged.append(
                        {
                            "timestamp": scan.scanDatetime.isoformat(),
                            "event": scan.scanEventCode,
                            "location": _format_location(
                                scan.scanFacilityCity,
                                scan.scanFacilityState,
                                scan.scanFacilityZip,
                            ),
                            "source": "live",
                        }
                    )

            stored = await _db_call(lock, db.get_scan_events, conn, imb_key)
            for row_data in stored:
                payload = row_data.get("event") or {}
                scan_dt = row_data.get("scan_datetime") or payload.get("scanDatetime")
                event_code = payload.get("scanEventCode") or "SCAN"
                key = f"{scan_dt}|{event_code}"
                if key in seen:
                    continue
                seen.add(key)
                merged.append(
                    {
                        "timestamp": scan_dt,
                        "event": event_code,
                        "location": _format_location(
                            payload.get("scanFacilityCity"),
                            payload.get("scanFacilityState"),
                            payload.get("scanFacilityZip"),
                        ),
                        "source": "stored",
                    }
                )

            merged.sort(key=lambda s: s.get("timestamp") or "", reverse=True)
            await websocket.send_json({"scans": merged})
    except WebSocketDisconnect:
        return


def _format_location(city: str | None, state: str | None, zip_code: str | None) -> str | None:
    """Join city/state/ZIP with single spaces; return None if all empty."""
    parts = [p for p in (city, state, zip_code) if p]
    return " ".join(parts) if parts else None


@router.post("/usps_feed")
async def post_usps_feed(
    payload: PushFeedPayload,
    locked: tuple[sqlite3.Connection, asyncio.Lock] = Depends(get_db_locked),
) -> JSONResponse:
    """Ingest IV-MTR push-feed events.

    IP allowlisting is enforced by :class:`mailwatch.middleware.IPAllowlistMiddleware`
    before this handler runs. Only letter scans (``handlingEventType == "L"``)
    are persisted; other event types are dropped.
    """
    conn, lock = locked
    accepted = 0
    new = 0
    for event in payload.events:
        if event.handlingEventType != "L" or not event.imb:
            continue
        accepted += 1
        event_json = event.model_dump_json().encode("utf-8")
        inserted: bool = await _db_call(
            lock,
            db.store_scan_event,
            conn,
            event.eventId,
            event.imb,
            event_json,
            event.scanDatetime.isoformat(),
        )
        if inserted:
            new += 1

    return JSONResponse({"accepted": accepted, "new": new})
