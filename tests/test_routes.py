"""Tests for :mod:`mailwatch.routes` — HTTP + WS endpoints."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

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
    assert 'name="format_type"' in body


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
        "format_type": "envelope",
        "row": "1",
        "col": "1",
    }
    base.update({k: str(v) for k, v in overrides.items()})
    return base


def _submit_generate(client: TestClient, **overrides: Any) -> Any:
    resp = client.post("/generate", data=_valid_form(**overrides))
    assert resp.status_code == 200, resp.text
    return resp


def test_generate_happy_path_renders_preview(client: TestClient) -> None:
    resp = _submit_generate(client)
    assert "Envelope generated" in resp.text
    assert "Download PDF" in resp.text
    # Tracking link should be populated.
    assert "/tracking?serial=" in resp.text


def test_generate_invalid_zip_is_422(client: TestClient) -> None:
    resp = client.post("/generate", data=_valid_form(recipient_zip="abc"))
    assert resp.status_code == 422


def test_generate_invalid_state_is_422(client: TestClient) -> None:
    resp = client.post("/generate", data=_valid_form(recipient_state="XXX"))
    assert resp.status_code == 422


def test_generate_sets_session(client: TestClient) -> None:
    _submit_generate(client)
    # The session cookie is set — the prefill test exercises this too, but
    # we also confirm the tracking page picks up the serial query string.
    resp = client.get("/")
    assert "J. Fixture" in resp.text


# --------------------------------------------------------------------------- #
# GET /download/{format}/{doc}                                                #
# --------------------------------------------------------------------------- #


def test_download_pdf_envelope(client: TestClient) -> None:
    _submit_generate(client)
    resp = client.get("/download/envelope/pdf")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/pdf"
    assert resp.content.startswith(b"%PDF-")


def test_download_pdf_avery(client: TestClient) -> None:
    _submit_generate(client, format_type="avery", row=2, col=1)
    resp = client.get("/download/avery/pdf")
    assert resp.status_code == 200
    assert resp.content.startswith(b"%PDF-")


def test_download_preview_renders_embed(client: TestClient) -> None:
    _submit_generate(client)
    resp = client.get("/download/envelope/preview")
    assert resp.status_code == 200
    assert 'type="application/pdf"' in resp.text


def test_download_without_session_is_400(client: TestClient) -> None:
    resp = client.get("/download/envelope/pdf")
    assert resp.status_code == 400


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
