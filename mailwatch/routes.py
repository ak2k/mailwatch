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
from dataclasses import dataclass
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

# Practical ceiling on any single session field so the cookie stays under
# the ~4KB browser limit even after JSON + itsdangerous signature overhead.
_MAX_FIELD_LEN = 500
_MAX_SENDER_LEN = 1000

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

    ``max_length`` on every field keeps the serialised session cookie under
    the browser's ~4KB ceiling — without it, a paste into the sender
    textarea could grow the signed cookie past the limit, the browser
    would silently drop ``Set-Cookie``, and every subsequent ``/preview``
    would hit the empty-session 400 branch with no log signal.

    Output options (envelope size, Avery part, row/col, human-readable,
    etc.) are NOT on this form — they live on the preview page as query
    parameters, so changing them regenerates the PDF without touching
    the session or the allocated IMb serial.
    """

    sender_address: str = Field(..., min_length=1, max_length=_MAX_SENDER_LEN)
    recipient_name: str = Field(..., min_length=1, max_length=_MAX_FIELD_LEN)
    recipient_company: str | None = Field(default=None, max_length=_MAX_FIELD_LEN)
    recipient_street: str = Field(..., min_length=1, max_length=_MAX_FIELD_LEN)
    recipient_address2: str | None = Field(default=None, max_length=_MAX_FIELD_LEN)
    recipient_city: str = Field(..., min_length=1, max_length=_MAX_FIELD_LEN)
    recipient_state: str = Field(..., min_length=2, max_length=2, pattern=r"^[A-Za-z]{2}$")
    recipient_zip: str = Field(..., pattern=_ZIP_PATTERN)
    # USPS deliveryPoint (2-digit) returned by /validate_address. When
    # present, it gets appended to the ZIP+4 to form a ZIP-11 IMb routing
    # code — the most specific routing USPS supports, used by IV-MTR for
    # per-delivery-point scan resolution. Always optional; the form's JS
    # populates it on a successful standardize and clears it on any
    # subsequent address edit.
    delivery_point: str | None = Field(default=None, pattern=r"^\d{2}$")


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


_ZIP_PLUS_4_LEN = 9  # 5-digit ZIP + 4-digit USPS extension
_DELIVERY_POINT_LEN = 2  # USPS 2-digit delivery-point suffix


def _build_routing(zip_value: str, delivery_point: str | None) -> str:
    """Build the 5/9/11-digit IMb routing code for a recipient.

    USPS-B-3200 allows four lengths: 0 (no routing), 5 (ZIP), 9 (ZIP+4),
    or 11 (ZIP+4 + 2-digit deliveryPoint). Longer = more delivery
    specificity in IV-MTR scans.

    - 5-digit ZIP or untrusted input → 5-digit routing
    - ZIP+4 (9 digits) → 9-digit routing
    - ZIP+4 + 2-digit deliveryPoint → 11-digit routing (best)

    ``delivery_point`` is only trusted if it came alongside a ZIP+4 that
    USPS itself emitted (the /validate_address JS pairs them). A mismatch
    (e.g. stale deliveryPoint + a user-edited ZIP) is a silent-miscode
    hazard, so we only emit 11-digit routing when we have 9 digits of
    ZIP in hand.
    """
    digits = _clean_zip(zip_value)
    if (
        len(digits) == _ZIP_PLUS_4_LEN
        and delivery_point
        and len(delivery_point) == _DELIVERY_POINT_LEN
        and delivery_point.isdigit()
    ):
        return digits + delivery_point
    return digits


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


@dataclass(frozen=True)
class MailPiece:
    """The piece-of-mail identity held in the session cookie.

    Allocated by :func:`post_generate`, read by :func:`get_preview` and
    :func:`get_download_pdf`. Stable across regen — the serial is minted
    once and re-used for every output-option change.
    """

    sender_address: str
    recipient: dict[str, Any]
    serial: int
    tracking: str
    routing: str

    @property
    def recipient_zip(self) -> str:
        return str(self.recipient["zip"])

    def sender_lines(self) -> list[str]:
        """Split the sender textarea into non-empty display lines."""
        return [line.strip() for line in self.sender_address.splitlines() if line.strip()]

    def recipient_lines(self) -> list[str]:
        """Render the recipient dict as display lines."""
        r = self.recipient
        lines: list[str] = [r["name"]]
        if r.get("company"):
            lines.append(r["company"])
        lines.append(r["street"])
        if r.get("address2"):
            lines.append(r["address2"])
        lines.append(f"{r['city']}, {r['state']} {r['zip']}")
        return lines


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


async def _standardize_for_generate(
    form: GenerateForm, new_api: NewApiClient
) -> tuple[dict[str, Any], str | None]:
    """Best-effort USPS standardization for the ``/generate`` path.

    Returns the ``(recipient_dict, delivery_point)`` pair that /generate
    should persist to the session and pass to :func:`_build_routing`. On
    USPS failure (network, upstream 5xx, bad address), falls back to the
    user's raw form input so a transient outage doesn't block mail
    generation — the envelope will print with unstandardized text + a
    5-digit routing code, which is still valid mail.

    Any `delivery_point` override from the client form is preferred only
    as a fallback for the USPS-failure case — when USPS returns a fresh
    address, we trust its freshly-emitted deliveryPoint and ignore any
    stale hidden-input value.
    """
    raw_recipient: dict[str, Any] = {
        "name": form.recipient_name,
        "company": form.recipient_company,
        "street": form.recipient_street,
        "address2": form.recipient_address2,
        "city": form.recipient_city,
        "state": form.recipient_state,
        "zip": form.recipient_zip,
    }
    try:
        addr_req = AddressRequest(
            firm=form.recipient_company or None,
            streetAddress=form.recipient_street,
            secondaryAddress=form.recipient_address2 or None,
            city=form.recipient_city,
            state=form.recipient_state,
            ZIPCode=_clean_zip(form.recipient_zip)[:5],
        )
        std = await new_api.validate_address(addr_req)
    except Exception as exc:  # noqa: BLE001 — intentional fallback on any USPS error
        logger.warning("standardize failed, using raw form input: %s", exc)
        return raw_recipient, form.delivery_point

    standardized: dict[str, Any] = {
        "name": form.recipient_name,
        "company": std.firm or std.address.firm or form.recipient_company,
        "street": std.address.streetAddress,
        "address2": std.address.secondaryAddress,
        "city": std.address.city,
        "state": std.address.state,
        "zip": std.full_zip,
    }
    fresh_dp: str | None = None
    if std.additionalInfo and std.additionalInfo.deliveryPoint:
        candidate = std.additionalInfo.deliveryPoint
        if len(candidate) == 2 and candidate.isdigit():
            fresh_dp = candidate
    return standardized, fresh_dp


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
    delivery_point: Annotated[str | None, Form()] = None,
    settings: Settings = Depends(get_settings_dep),
    locked: tuple[sqlite3.Connection, asyncio.Lock] = Depends(get_db_locked),
    new_api: NewApiClient = Depends(get_new_api),
) -> Response:
    """Allocate a serial, store mail piece in session, redirect to ``/preview``.

    Server-side USPS standardization runs unconditionally so the envelope
    always prints with OCR-compliant text and the IMb routing picks up
    ZIP+4 + deliveryPoint (11-digit routing) when USPS knows them. The
    "Validate address" button on the form remains as an optional preview
    for users who want to see the standardized shape before submitting;
    an unvalidated submit gets the same standardization on the server
    path. On USPS failure (outage, bad address) we fall back to the
    user's raw input so a flaky upstream doesn't block mail generation.

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
            # Empty-string from form → None so Pydantic's pattern skips.
            delivery_point=delivery_point or None,
        )
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.errors()) from exc

    recipient, effective_dp = await _standardize_for_generate(form, new_api)

    conn, lock = locked
    bucket = _day_bucket()
    serial: int = await _db_call(lock, db.next_serial, conn, bucket)

    tracking = _build_tracking(settings, serial)
    routing = _build_routing(str(recipient["zip"]), effective_dp)
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
    request.session["recipient"] = recipient
    request.session["serial"] = serial
    request.session["tracking"] = tracking
    request.session["routing"] = routing

    # 303 = "see other" — turns the POST into a GET for the redirect target,
    # so refreshing the preview page doesn't resubmit the form. The preview
    # page lets the user pick envelope vs Avery via its own format radio,
    # so /generate doesn't need to carry a format choice through.
    return RedirectResponse(
        url="/preview",
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
#  - Cookies are shared across same-origin tabs. Two tabs in one browser
#    SHARE the session cookie — the last /generate wins, and both tabs
#    render the most recent piece of mail on their next request. If you
#    need to work on two pieces concurrently, use two browser profiles.
#  - Serial stability: allocated once at /generate, preserved across
#    every /preview option change. Only "Edit recipient" → /generate
#    allocates a fresh serial.


def _require_session_piece(request: Request) -> MailPiece:
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
    return MailPiece(
        sender_address=sender_address,
        recipient=recipient,
        serial=int(serial),
        tracking=str(tracking),
        routing=str(routing or ""),
    )


@dataclass(frozen=True)
class PreviewOptions:
    """Validated, clamped output options shared by /preview and /download/pdf.

    Membership of ``size`` / ``part`` is verified; ``row`` / ``col`` are
    clamped into the currently-chosen Avery grid so a stale URL from a
    bigger-grid part (e.g. 5167's 4x20) can't 500 the render on a smaller
    part (8163's 2x5). ``hr`` is the last-value-wins result of parsing
    the ``hr`` query param — see :func:`_parse_bool_last`.
    """

    fmt: Literal["envelope", "avery"]
    size: str
    part: str
    mode: Literal["single", "fill"]
    row: int
    col: int
    hr: bool

    def query_string(self) -> str:
        """Render the options back into a canonical query string for the PDF URL."""
        return urlencode(
            {
                "fmt": self.fmt,
                "size": self.size,
                "part": self.part,
                "mode": self.mode,
                "row": self.row,
                "col": self.col,
                "hr": "1" if self.hr else "0",
            }
        )


def _parse_bool_last(values: list[str]) -> bool:
    """Read the last value in a list of ``hr=0``/``hr=1`` query params.

    The options form emits a hidden ``hr=0`` *before* the checkbox and the
    checkbox itself sends ``hr=1`` when checked, so the browser submits
    ``?hr=0`` when unchecked and ``?hr=0&hr=1`` when checked. Starlette's
    default single-value query binding picks the *first* value, which
    inverts the checkbox — so this helper deliberately takes the *last*.
    An empty list (no form submission yet, first visit) defaults to True.
    """
    if not values:
        return True
    return values[-1] == "1"


def _preview_options(
    request: Request,
    fmt: Literal["envelope", "avery"] = "envelope",
    size: str = DEFAULT_ENVELOPE,
    part: str = DEFAULT_AVERY,
    mode: Literal["single", "fill"] = "fill",
    row: int = 1,
    col: int = 1,
) -> PreviewOptions:
    """FastAPI dep: parse, validate, clamp the output-options query string."""
    if size not in ENVELOPES:
        raise HTTPException(status_code=400, detail=f"unknown envelope size: {size}")
    if part not in AVERY:
        raise HTTPException(status_code=400, detail=f"unknown Avery part: {part}")
    tpl = AVERY[part]
    clamped_row = max(1, min(row, tpl.rows))
    clamped_col = max(1, min(col, tpl.cols))
    hr_on = _parse_bool_last(request.query_params.getlist("hr"))
    return PreviewOptions(
        fmt=fmt,
        size=size,
        part=part,
        mode=mode,
        row=clamped_row,
        col=clamped_col,
        hr=hr_on,
    )


@router.get("/preview", response_class=HTMLResponse)
async def get_preview(
    request: Request,
    piece: MailPiece = Depends(_require_session_piece),
    opts: PreviewOptions = Depends(_preview_options),
) -> Response:
    """Render the preview page with the current output options.

    All option state lives in the URL — changing a radio / select in the
    preview form navigates back to this route with different query params
    and the PDF embed reloads. No POST, no session mutation, no new
    serial.
    """
    tpl = AVERY[opts.part]
    pdf_url = f"/download/pdf?{opts.query_string()}"
    tracking_url = f"/tracking?serial={piece.serial}&zip={piece.recipient_zip}"
    return templates.TemplateResponse(
        request,
        "preview.html",
        {
            "serial": piece.serial,
            "recipient_zip": piece.recipient_zip,
            "fmt": opts.fmt,
            "size": opts.size,
            "part": opts.part,
            "mode": opts.mode,
            "row": opts.row,
            "col": opts.col,
            "hr": opts.hr,
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
    piece: MailPiece = Depends(_require_session_piece),
    opts: PreviewOptions = Depends(_preview_options),
) -> Response:
    """Render the session's mail piece as a PDF using the given options."""
    buf = io.BytesIO()
    if opts.fmt == "envelope":
        await asyncio.to_thread(
            pdf.render_envelope,
            piece.sender_lines(),
            piece.recipient_lines(),
            piece.tracking,
            piece.routing,
            buf,
            envelope_size=opts.size,
            human_readable=opts.hr,
        )
        filename = f"envelope-{piece.serial}.pdf"
    else:
        label: pdf.LabelData = {
            "recipient": piece.recipient_lines(),
            "tracking": piece.tracking,
            "routing": piece.routing,
        }
        await asyncio.to_thread(
            pdf.render_avery,
            label,
            buf,
            part=opts.part,
            mode=opts.mode,
            start_row=opts.row,
            start_col=opts.col,
            human_readable=opts.hr,
        )
        filename = f"avery-{opts.part}-{piece.serial}.pdf"

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
