"""Tests for :mod:`mailwatch.routes` — HTTP + WS endpoints."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from mailwatch.app import create_app
from mailwatch.config import Settings
from mailwatch.models import (
    AdditionalInfo,
    AddressInfo,
    StandardizedAddressResponse,
    TrackingData,
    TrackingResponse,
    TrackingScan,
)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        MAILER_ID=123456,
        BSG_USERNAME="bsg-user",
        BSG_PASSWORD="bsg-pw",
        USPS_NEWAPI_CUSTOMER_ID="client-abc",
        USPS_NEWAPI_CUSTOMER_SECRET="client-secret",
        SESSION_KEY="a" * 32,
        DB_PATH=tmp_path / "mailwatch.db",
        USPS_FEED_CIDRS=["56.0.0.0/8", "127.0.0.0/8"],  # allow testclient's 127.0.0.1
    )


@pytest.fixture(autouse=True)
def _no_real_token_refresh(monkeypatch: pytest.MonkeyPatch) -> None:
    """Park the refresh loop; the routes tests don't exercise token fetches."""
    monkeypatch.setattr("mailwatch.app.TOKEN_REFRESH_INTERVAL_SEC", 3600)


@pytest.fixture
def fake_standardized() -> StandardizedAddressResponse:
    return StandardizedAddressResponse(
        firm=None,
        address=AddressInfo(
            streetAddress="1600 PENNSYLVANIA AVE NW",
            city="WASHINGTON",
            state="DC",
            ZIPCode="20500",
            ZIPPlus4="0005",
        ),
        additionalInfo=AdditionalInfo(deliveryPoint="99", DPVConfirmation="Y"),
    )


@pytest.fixture
def fake_tracking() -> TrackingResponse:
    return TrackingResponse(
        data=TrackingData(
            imb="9" * 31,
            scans=[
                TrackingScan(
                    imb="9" * 31,
                    scanDatetime="2026-04-21T09:00:00Z",  # type: ignore[arg-type]
                    scanEventCode="SD",
                    scanFacilityCity="WASHINGTON",
                    scanFacilityState="DC",
                    scanFacilityZip="20018",
                )
            ],
        )
    )


@pytest.fixture
def client(
    settings: Settings,
    fake_standardized: StandardizedAddressResponse,
    fake_tracking: TrackingResponse,
) -> Iterator[TestClient]:
    """TestClient with ``new_api`` and ``ivmtr`` swapped for AsyncMocks.

    The swap happens inside the ``with`` block (after lifespan startup)
    so we replace live-built clients rather than pre-empt construction.
    """
    app = create_app(settings, session_https_only=False)
    # ``client=("127.0.0.1", ...)`` so ``IPAllowlistMiddleware`` (which
    # reads ``request.client.host``) sees a real IP — the TestClient
    # default ``"testclient"`` isn't parseable as an IP and would 403.
    with TestClient(app, client=("127.0.0.1", 12345)) as c:
        app.state.new_api.validate_address = AsyncMock(return_value=fake_standardized)  # type: ignore[method-assign]
        app.state.ivmtr.get_tracking = AsyncMock(return_value=fake_tracking)  # type: ignore[method-assign]
        yield c


# --------------------------------------------------------------------------- #
# GET /                                                                        #
# --------------------------------------------------------------------------- #


def test_index_renders(client: TestClient) -> None:
    resp = client.get("/")
    assert resp.status_code == 200
    body = resp.text
    assert '<form id="generate-form"' in body
    assert 'name="sender_address"' in body
    # Format choice lives on /preview now, not page 1.
    assert 'name="format_type"' not in body


def test_index_prefills_sender_from_session(client: TestClient) -> None:
    """Sender text persists across requests via the session cookie."""
    _submit_generate(client)
    resp = client.get("/")
    assert resp.status_code == 200
    # HTML-escaped apostrophes etc. don't appear in our fixture; plain match OK.
    assert "J. Fixture" in resp.text


# --------------------------------------------------------------------------- #
# POST /generate                                                              #
# --------------------------------------------------------------------------- #


def _valid_form(**overrides: Any) -> dict[str, Any]:
    base = {
        "sender_address": "J. Fixture\n1 Sender Ln\nBoston MA 02110",
        "recipient_name": "Mail Recipient",
        "recipient_company": "",
        "recipient_street": "1600 Pennsylvania Ave NW",
        "recipient_address2": "",
        "recipient_city": "Washington",
        "recipient_state": "DC",
        "recipient_zip": "20500",
    }
    base.update({k: str(v) for k, v in overrides.items()})
    return base


def _submit_generate(client: TestClient, **overrides: Any) -> Any:
    """POST /generate and follow its 303 to /preview. Returns the preview response."""
    resp = client.post("/generate", data=_valid_form(**overrides), follow_redirects=True)
    assert resp.status_code == 200, resp.text
    return resp


def test_generate_redirects_to_preview(client: TestClient) -> None:
    """POST /generate returns 303 → /preview (no HTML body).

    Format choice happens on /preview, so /generate doesn't need to carry
    a fmt query param through — /preview defaults to fmt=envelope.
    """
    resp = client.post("/generate", data=_valid_form(), follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/preview"


def test_generate_happy_path_preview_has_embed(client: TestClient) -> None:
    resp = _submit_generate(client)
    assert "Preview &amp; download" in resp.text or "Preview & download" in resp.text
    assert 'type="application/pdf"' in resp.text
    assert "/tracking?serial=" in resp.text


def test_generate_invalid_zip_is_422(client: TestClient) -> None:
    resp = client.post("/generate", data=_valid_form(recipient_zip="abc"))
    assert resp.status_code == 422


def test_generate_invalid_state_is_422(client: TestClient) -> None:
    resp = client.post("/generate", data=_valid_form(recipient_state="XXX"))
    assert resp.status_code == 422


def test_generate_sets_session(client: TestClient) -> None:
    _submit_generate(client)
    resp = client.get("/")
    assert "J. Fixture" in resp.text


def _routing_from_generate(client: TestClient, **form_overrides: Any) -> str:
    """Invoke /generate and return the routing string passed into imb.encode.

    Patching the module-level imb.encode lets us observe what /generate
    actually feeds the IMb encoder without parsing compressed PDF bytes.
    """
    with patch("mailwatch.routes.imb.encode", wraps=__import__("mailwatch.imb", fromlist=["encode"]).encode) as spy:
        resp = client.post(
            "/generate",
            data=_valid_form(**form_overrides),
            follow_redirects=False,
        )
        assert resp.status_code == 303, resp.text
        # imb.encode(barcode_id, srv_type, mailer_id, serial, routing)
        return str(spy.call_args.args[4])


def test_generate_uses_standardized_routing_11digit(client: TestClient) -> None:
    """USPS standardization succeeds → routing uses USPS ZIP+4 + deliveryPoint.

    The client fixture mocks validate_address to return ZIP 20500,
    ZIPPlus4 0005, deliveryPoint 99 regardless of what the user typed.
    """
    # Form input is 5-digit ZIP + no delivery_point — USPS response trumps.
    assert _routing_from_generate(client) == "20500000599"


def test_generate_uses_standardized_routing_ignores_stale_form_dp(
    client: TestClient,
) -> None:
    """USPS's freshly-returned deliveryPoint beats any stale form hidden input.

    A client submitting a mismatched ``delivery_point`` must not make the
    server mis-encode: a successful standardize always wins.
    """
    assert _routing_from_generate(client, delivery_point="23") == "20500000599"


def test_generate_falls_back_to_form_zip_when_usps_fails(
    client: TestClient,
) -> None:
    """USPS outage → routing falls back to user form input.

    5-digit ZIP + no DP → 5-digit routing.
    """
    client.app.state.new_api.validate_address = AsyncMock(
        side_effect=RuntimeError("USPS down")
    )
    assert _routing_from_generate(client, recipient_zip="20500") == "20500"


def test_generate_fallback_uses_form_zip_plus_4(client: TestClient) -> None:
    """USPS outage with ZIP+4 form input → 9-digit routing from the user's hyphen."""
    client.app.state.new_api.validate_address = AsyncMock(
        side_effect=RuntimeError("USPS down")
    )
    assert (
        _routing_from_generate(client, recipient_zip="20500-0001") == "205000001"
    )


def test_generate_fallback_uses_form_delivery_point(client: TestClient) -> None:
    """USPS outage + form-provided delivery_point → 11-digit routing.

    The stale-deliveryPoint protection (only trust DP paired with a ZIP+4)
    still applies on the fallback path — the /validate_address JS already
    clears delivery_point when ZIP is edited, so a form-carried DP reflects
    a previously-successful validate.
    """
    client.app.state.new_api.validate_address = AsyncMock(
        side_effect=RuntimeError("USPS down")
    )
    assert (
        _routing_from_generate(
            client, recipient_zip="20500-0001", delivery_point="23"
        )
        == "20500000123"
    )


def test_generate_fallback_drops_stale_dp_with_zip5(client: TestClient) -> None:
    """USPS outage + 5-digit ZIP + stale DP → 5-digit routing (DP dropped)."""
    client.app.state.new_api.validate_address = AsyncMock(
        side_effect=RuntimeError("USPS down")
    )
    assert (
        _routing_from_generate(client, recipient_zip="20500", delivery_point="23")
        == "20500"
    )


def test_generate_standardizes_session_recipient(client: TestClient) -> None:
    """Session-stored recipient should be the USPS standardized shape.

    The fixture returns uppercase "1600 PENNSYLVANIA AVE NW" / "WASHINGTON"
    / ZIP "20500-0005". After /generate, a follow-up GET / should pre-fill
    the form with those standardized values rather than the raw input.
    """
    _submit_generate(client, recipient_street="1600 Pennsylvania Ave NW")
    resp = client.get("/")
    assert resp.status_code == 200
    assert "1600 PENNSYLVANIA AVE NW" in resp.text
    assert "20500-0005" in resp.text


def test_generate_rejects_malformed_delivery_point(client: TestClient) -> None:
    """A non-2-digit deliveryPoint from a hostile or broken client → 422."""
    resp = client.post(
        "/generate",
        data=_valid_form(delivery_point="abc"),
        follow_redirects=False,
    )
    assert resp.status_code == 422


# --------------------------------------------------------------------------- #
# GET /preview + GET /download/pdf                                            #
# --------------------------------------------------------------------------- #


def test_preview_without_session_is_400(client: TestClient) -> None:
    resp = client.get("/preview")
    assert resp.status_code == 400


def test_preview_default_query_renders(client: TestClient) -> None:
    _submit_generate(client)
    resp = client.get("/preview", params={"fmt": "envelope"})
    assert resp.status_code == 200
    assert 'src="/download/pdf?' in resp.text
    # The PDF URL should reflect the defaults so the embed has a usable target.
    assert "size=%2310" in resp.text  # "#10" URL-encoded
    assert "part=8163" in resp.text


def test_preview_rejects_unknown_size(client: TestClient) -> None:
    _submit_generate(client)
    resp = client.get("/preview", params={"fmt": "envelope", "size": "#99"})
    assert resp.status_code == 400


def test_preview_rejects_unknown_part(client: TestClient) -> None:
    _submit_generate(client)
    resp = client.get("/preview", params={"fmt": "avery", "part": "9999"})
    assert resp.status_code == 400


def test_download_pdf_envelope_default(client: TestClient) -> None:
    _submit_generate(client)
    resp = client.get("/download/pdf", params={"fmt": "envelope"})
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/pdf"
    assert resp.content.startswith(b"%PDF-")


def test_download_pdf_envelope_different_size(client: TestClient) -> None:
    _submit_generate(client)
    resp = client.get("/download/pdf", params={"fmt": "envelope", "size": "A7"})
    assert resp.status_code == 200
    assert resp.content.startswith(b"%PDF-")


def test_preview_clamps_stale_row_col_into_chosen_grid(client: TestClient) -> None:
    """A URL built against 5167 (4x20) must not 500 when switched to 8163 (2x5)."""
    _submit_generate(client)
    resp = client.get(
        "/preview",
        params={"fmt": "avery", "part": "8163", "row": 999, "col": 999},
    )
    assert resp.status_code == 200, resp.text
    # Rebuilt PDF URL should carry the clamped values (2, 5).
    assert "row=5" in resp.text
    assert "col=2" in resp.text


def test_download_pdf_clamps_stale_row_col(client: TestClient) -> None:
    _submit_generate(client)
    resp = client.get(
        "/download/pdf",
        params={"fmt": "avery", "part": "8163", "row": 999, "col": 999},
    )
    assert resp.status_code == 200
    assert resp.content.startswith(b"%PDF-")


def test_download_pdf_avery(client: TestClient) -> None:
    _submit_generate(client)
    resp = client.get(
        "/download/pdf",
        params={"fmt": "avery", "part": "8163", "mode": "fill", "row": 2, "col": 1},
    )
    assert resp.status_code == 200
    assert resp.content.startswith(b"%PDF-")


def test_download_pdf_avery_other_part(client: TestClient) -> None:
    _submit_generate(client)
    resp = client.get(
        "/download/pdf",
        params={"fmt": "avery", "part": "5160", "mode": "fill"},
    )
    assert resp.status_code == 200
    assert resp.content.startswith(b"%PDF-")


def test_download_pdf_without_session_is_400(client: TestClient) -> None:
    resp = client.get("/download/pdf", params={"fmt": "envelope"})
    assert resp.status_code == 400


def test_hr_flag_toggles(client: TestClient) -> None:
    """hr=0 renders a valid PDF just like hr=1; the difference is visual only."""
    _submit_generate(client)
    on = client.get("/download/pdf", params={"fmt": "envelope", "hr": "1"})
    off = client.get("/download/pdf", params={"fmt": "envelope", "hr": "0"})
    assert on.status_code == 200
    assert off.status_code == 200
    # Same serial + recipient → PDFs should differ only because of the hr toggle.
    assert on.content != off.content


def test_hr_checkbox_off_round_trip_through_form(client: TestClient) -> None:
    """Unchecking the HR checkbox must actually persist through /preview → /download/pdf.

    Regression guard for the checkbox-off-silently-reverted bug: the form
    emits a hidden ``hr=0`` before the checkbox and the checkbox itself
    emits ``hr=1`` when checked. ``_parse_bool_last`` takes the last
    value, so unchecked (``?hr=0``) stays off and checked (``?hr=0&hr=1``)
    is on.
    """
    _submit_generate(client)

    # Simulate browser submit of the form with checkbox UNCHECKED — only hr=0 sent.
    resp_off = client.get("/preview?fmt=envelope&size=%2310&hr=0")
    assert resp_off.status_code == 200
    # Rebuilt PDF URL should reflect hr=0, and the checkbox should NOT be checked.
    assert "hr=0" in resp_off.text
    assert 'name="hr" value="1" checked' not in resp_off.text

    # Simulate checked: both hidden hr=0 AND checkbox hr=1, in form order.
    resp_on = client.get("/preview?fmt=envelope&size=%2310&hr=0&hr=1")
    assert resp_on.status_code == 200
    assert "hr=1" in resp_on.text
    assert 'value="1" checked' in resp_on.text


def test_parse_bool_last_unit() -> None:
    """_parse_bool_last: empty → True (first visit); else last value wins."""
    from mailwatch.routes import _parse_bool_last

    assert _parse_bool_last([]) is True
    assert _parse_bool_last(["0"]) is False
    assert _parse_bool_last(["1"]) is True
    assert _parse_bool_last(["0", "1"]) is True  # checked: hidden + checkbox
    assert _parse_bool_last(["1", "0"]) is False
    assert _parse_bool_last(["true"]) is False  # narrow surface — only "1" is truthy
    assert _parse_bool_last(["yes"]) is False


def test_build_routing_unit() -> None:
    """_build_routing: 5 / 9 / 11-digit routing based on inputs.

    An 11-digit routing is only emitted when we have a ZIP+4 *and* a
    trusted 2-digit deliveryPoint — a bare deliveryPoint paired with a
    5-digit ZIP is not trusted (would silently miscode).
    """
    from mailwatch.routes import _build_routing

    assert _build_routing("20500", None) == "20500"
    assert _build_routing("20500-0001", None) == "205000001"
    assert _build_routing("20500-0001", "23") == "20500000123"
    # Stale deliveryPoint dropped when ZIP is only 5 digits.
    assert _build_routing("20500", "23") == "20500"
    # Malformed deliveryPoint ignored.
    assert _build_routing("20500-0001", "") == "205000001"
    assert _build_routing("20500-0001", "2") == "205000001"
    assert _build_routing("20500-0001", "abc") == "205000001"
    # Hyphen stripped; whitespace tolerated.
    assert _build_routing("  20500 ", None) == "20500"


# --------------------------------------------------------------------------- #
# Regen stability — changing output options must NOT allocate a new serial    #
# --------------------------------------------------------------------------- #


def test_preview_regen_keeps_same_serial(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Changing size / part / mode / hr must not mint a new IMb serial.

    Couples the test to the actual invariant ("``db.next_serial`` called
    exactly once") instead of a regex on the rendered tracking URL — so
    a later change to the tracking-link HTML format doesn't silently
    pass this check.
    """
    from mailwatch import db, routes

    call_count = {"n": 0}
    real_next_serial = db.next_serial

    def counting_next_serial(conn: Any, bucket: int) -> int:
        call_count["n"] += 1
        return real_next_serial(conn, bucket)

    monkeypatch.setattr(routes.db, "next_serial", counting_next_serial)

    _submit_generate(client)
    assert call_count["n"] == 1, "initial /generate should allocate exactly one serial"

    # N regen clicks across envelope sizes + Avery parts — none should re-call next_serial.
    for params in [
        {"fmt": "envelope", "size": "A7"},
        {"fmt": "envelope", "size": "#11"},
        {"fmt": "envelope", "size": "#6_3_4"},
        {"fmt": "avery", "part": "5163"},
        {"fmt": "avery", "part": "8163", "mode": "single"},
        {"fmt": "envelope", "hr": "0"},
    ]:
        resp = client.get("/preview", params=params)
        assert resp.status_code == 200, (params, resp.text)

    assert (
        call_count["n"] == 1
    ), f"/preview must not reallocate serials; got {call_count['n']} total calls"


# --------------------------------------------------------------------------- #
# POST /validate_address                                                      #
# --------------------------------------------------------------------------- #


def test_validate_address_returns_standardized(client: TestClient) -> None:
    body = {
        "streetAddress": "1600 Pennsylvania Ave NW",
        "city": "Washington",
        "state": "DC",
        "ZIPCode": "20500",
    }
    resp = client.post("/validate_address", json=body)
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["standardized"]["ZIPCode"] == "20500"
    assert payload["standardized"]["ZIPPlus4"] == "0005"
    assert payload["standardized"]["full_zip"] == "20500-0005"
    # The mocked client should have been called once.
    client.app.state.new_api.validate_address.assert_awaited_once()


def test_validate_address_invalid_zip_is_422(client: TestClient) -> None:
    body = {
        "streetAddress": "1600 Pennsylvania Ave NW",
        "city": "Washington",
        "state": "DC",
        "ZIPCode": "abc",
    }
    resp = client.post("/validate_address", json=body)
    assert resp.status_code == 422


def test_validate_address_upstream_error_is_502(client: TestClient, settings: Settings) -> None:
    app = client.app
    app.state.new_api.validate_address = AsyncMock(side_effect=RuntimeError("USPS down"))
    body = {
        "streetAddress": "1 Main St",
        "city": "Springfield",
        "state": "IL",
        "ZIPCode": "62701",
    }
    resp = client.post("/validate_address", json=body)
    assert resp.status_code == 502
    assert "USPS down" in resp.json()["error"]


# --------------------------------------------------------------------------- #
# GET /tracking                                                                #
# --------------------------------------------------------------------------- #


def test_tracking_page_renders(client: TestClient) -> None:
    resp = client.get("/tracking")
    assert resp.status_code == 200
    assert "Track a letter" in resp.text


def test_tracking_page_prefills(client: TestClient) -> None:
    resp = client.get("/tracking", params={"serial": "42", "zip": "20500"})
    assert resp.status_code == 200
    assert 'value="42"' in resp.text
    assert 'value="20500"' in resp.text


# --------------------------------------------------------------------------- #
# WS /track-ws                                                                 #
# --------------------------------------------------------------------------- #


def test_track_ws_returns_merged_scans(client: TestClient) -> None:
    with client.websocket_connect("/track-ws") as ws:
        ws.send_text(json.dumps({"serial": 1, "receipt_zip": "20500"}))
        msg = ws.receive_json()
        assert "scans" in msg
        # Live fake returned 1 scan.
        assert len(msg["scans"]) >= 1
        assert msg["scans"][0]["event"] == "SD"
        assert msg["scans"][0]["location"].startswith("WASHINGTON")


def test_track_ws_rejects_invalid_payload(client: TestClient) -> None:
    with client.websocket_connect("/track-ws") as ws:
        ws.send_text("not json")
        msg = ws.receive_json()
        assert "error" in msg


def test_track_ws_rejects_bad_zip(client: TestClient) -> None:
    with client.websocket_connect("/track-ws") as ws:
        ws.send_text(json.dumps({"serial": 1, "receipt_zip": "abc"}))
        msg = ws.receive_json()
        assert "error" in msg


def test_track_ws_survives_live_pull_failure(client: TestClient) -> None:
    """If IV-MTR errors, the WS still responds with stored-only scans."""
    client.app.state.ivmtr.get_tracking = AsyncMock(side_effect=RuntimeError("IV-MTR down"))
    with client.websocket_connect("/track-ws") as ws:
        ws.send_text(json.dumps({"serial": 1, "receipt_zip": "20500"}))
        msg = ws.receive_json()
        # Empty live results, no stored events -> empty list.
        assert msg == {"scans": []}


def test_generate_with_mailer_id_starting_with_9(tmp_path: Path) -> None:
    """The 9-prefix Mailer ID branch builds a 9+6 tracking string."""
    settings = Settings(
        MAILER_ID=900000000,  # 9-digit, starts with 9
        BSG_USERNAME="u",
        BSG_PASSWORD="p",
        USPS_NEWAPI_CUSTOMER_ID="c",
        USPS_NEWAPI_CUSTOMER_SECRET="s",
        SESSION_KEY="a" * 32,
        DB_PATH=tmp_path / "mw.db",
        USPS_FEED_CIDRS=["127.0.0.0/8"],
    )
    app = create_app(settings, session_https_only=False)
    with TestClient(app, client=("127.0.0.1", 1)) as c:
        resp = c.post("/generate", data=_valid_form())
        assert resp.status_code == 200
        # Session should contain a 20-digit tracking with MID in the 9-digit slot.
        cookies = c.cookies
        assert "session" in cookies


def test_track_ws_merges_stored_events(client: TestClient, fake_tracking: TrackingResponse) -> None:
    """Webhook-stored events show up alongside the live pull."""
    from mailwatch import db as db_mod

    conn = client.app.state.db
    # Build the expected IMb key the route uses internally.
    settings: Settings = client.app.state.settings
    tracking_digits = (
        f"{settings.BARCODE_ID:02d}{settings.SRV_TYPE:03d}" f"{settings.MAILER_ID:06d}{7:09d}"
    )
    imb_key = f"{tracking_digits}20500"

    event_payload = {
        "eventId": "evt-1",
        "imb": imb_key,
        "handlingEventType": "L",
        "scanDatetime": "2026-04-20T08:00:00+00:00",
        "scanEventCode": "AC",
        "scanFacilityCity": "BOSTON",
        "scanFacilityState": "MA",
        "scanFacilityZip": "02110",
    }
    db_mod.store_scan_event(
        conn,
        "evt-1",
        imb_key,
        json.dumps(event_payload).encode(),
        "2026-04-20T08:00:00+00:00",
    )

    with client.websocket_connect("/track-ws") as ws:
        ws.send_text(json.dumps({"serial": 7, "receipt_zip": "20500"}))
        msg = ws.receive_json()
        events = [s["event"] for s in msg["scans"]]
        assert "AC" in events
        assert "SD" in events


# --------------------------------------------------------------------------- #
# POST /usps_feed                                                              #
# --------------------------------------------------------------------------- #


def _sample_push() -> dict[str, Any]:
    return {
        "events": [
            {
                "eventId": "push-1",
                "imb": "1" * 31,
                "handlingEventType": "L",
                "scanDatetime": "2026-04-21T10:00:00+00:00",
                "scanEventCode": "SD",
                "scanFacilityCity": "WASHINGTON",
                "scanFacilityState": "DC",
                "scanFacilityZip": "20018",
            },
            {
                # Container-level event — should be dropped.
                "eventId": "push-2",
                "imb": "",
                "handlingEventType": "C",
                "scanDatetime": "2026-04-21T10:00:00+00:00",
            },
        ]
    }


def test_usps_feed_stores_letter_events(client: TestClient) -> None:
    resp = client.post("/usps_feed", json=_sample_push())
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"accepted": 1, "new": 1}


def test_usps_feed_idempotent_on_duplicate(client: TestClient) -> None:
    client.post("/usps_feed", json=_sample_push())
    resp = client.post("/usps_feed", json=_sample_push())
    assert resp.status_code == 200
    assert resp.json() == {"accepted": 1, "new": 0}


def test_usps_feed_rejects_non_allowlisted_ip(tmp_path: Path) -> None:
    """A request from outside the configured CIDR set is 403'd."""
    narrow_settings = Settings(
        MAILER_ID=123456,
        BSG_USERNAME="bsg-user",
        BSG_PASSWORD="bsg-pw",
        USPS_NEWAPI_CUSTOMER_ID="client-abc",
        USPS_NEWAPI_CUSTOMER_SECRET="client-secret",
        SESSION_KEY="a" * 32,
        DB_PATH=tmp_path / "mailwatch.db",
        USPS_FEED_CIDRS=["56.0.0.0/8"],  # specifically exclude testclient's 127.0.0.1
    )
    app = create_app(narrow_settings)
    with TestClient(app) as c:
        resp = c.post("/usps_feed", json=_sample_push())
    assert resp.status_code == 403
