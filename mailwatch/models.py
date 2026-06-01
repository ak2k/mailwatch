"""Pydantic v2 data shapes for USPS APIs.

Covers the four USPS-adjacent wire formats mailwatch speaks:

1. OAuth token endpoints (two separate flows):
   - `POST apis.usps.com/oauth2/v3/token` — modern client_credentials grant
   - `POST services.usps.com/oauth/authenticate` — IV-MTR's legacy flow
2. Address standardization (`GET apis.usps.com/addresses/v3/address`)
3. IV-MTR tracking pulls (`GET iv.usps.com/.../api/mt/get/piece/imb/{imb}`)
4. IV-MTR push-feed webhook payloads delivered to us

All models set ``extra="ignore"``: USPS freely adds response fields, and the
contract we care about is "the fields we asked for are present and well-typed",
not "no other fields exist". Breaking on new fields would make every USPS
change a production outage.

Field names are camelCase / mixedCase throughout to match USPS's wire format
verbatim — parsing would need per-field ``alias=`` declarations otherwise.
N815 (mixedCase class-scope variables) and S105 (hardcoded-password false
positive on OAuth's ``token_type="Bearer"``) are disabled file-wide.
"""
# ruff: noqa: N815, S105

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, computed_field

# --------------------------------------------------------------------------- #
# OAuth token responses                                                       #
# --------------------------------------------------------------------------- #


class NewApiTokenResponse(BaseModel):
    """Response from ``POST apis.usps.com/oauth2/v3/token`` (client_credentials).

    USPS docs name the required fields ``access_token``, ``token_type``,
    ``expires_in``, and optionally ``scope`` / ``issued_at``. We keep the
    optional ones around so callers that want them can read them, but the
    happy path only needs the first three.

    Empirically (verified against a live apis.usps.com Public Access app,
    2026-04): ``issued_at`` comes back as an **integer** unix-milliseconds
    timestamp, not a string — USPS's own docs disagree with the deployed
    API. ``api_products`` comes back as a **list** of strings, not a single
    string. Typing both permissively so the model doesn't reject real
    responses.
    """

    model_config = ConfigDict(extra="ignore")

    access_token: str
    token_type: str = "Bearer"
    expires_in: int
    scope: str | None = None
    issued_at: int | str | None = None
    application_name: str | None = None
    api_products: list[str] | str | None = None


class IVMTRTokenResponse(BaseModel):
    """Response from ``POST services.usps.com/oauth/authenticate`` (IV-MTR).

    Legacy OAuth flow — spec says it returns a refresh_token alongside the
    access_token, but empirically (verified 2026-04) not every response
    carries one. Treat as optional so the model tolerates both shapes; the
    client re-authenticates with username+password if the refresh_token is
    absent on renewal.
    """

    model_config = ConfigDict(extra="ignore")

    access_token: str
    refresh_token: str | None = None
    token_type: str
    expires_in: int


class OAuthErrorResponse(BaseModel):
    """Standard OAuth2 error body.

    Both USPS token endpoints return this shape on failure, so we can parse
    4xx bodies uniformly and surface ``error_description`` to callers.
    """

    model_config = ConfigDict(extra="ignore")

    error: str
    error_description: str | None = None


# --------------------------------------------------------------------------- #
# Address standardization                                                     #
# --------------------------------------------------------------------------- #

_STATE_PATTERN = r"^[A-Z]{2}$"
_ZIP5_PATTERN = r"^\d{5}$"
_ZIP4_PATTERN = r"^\d{4}$"


class AddressRequest(BaseModel):
    """Query-string shape for ``GET apis.usps.com/addresses/v3/address``.

    USPS requires street + city + state + 5-digit ZIP; ``firm``,
    ``secondaryAddress``, and ``ZIPPlus4`` are optional hints that improve
    match quality when present.
    """

    model_config = ConfigDict(extra="ignore")

    firm: str | None = None
    streetAddress: str
    secondaryAddress: str | None = None
    city: str
    state: str = Field(pattern=_STATE_PATTERN, min_length=2, max_length=2)
    ZIPCode: str = Field(pattern=_ZIP5_PATTERN, min_length=5, max_length=5)
    ZIPPlus4: str | None = Field(default=None, pattern=_ZIP4_PATTERN)


class AddressInfo(BaseModel):
    """The ``address`` sub-object inside a standardization response.

    Shape matches :class:`AddressRequest`; validators are looser because USPS
    is the authoritative source here — we trust what they return.
    """

    model_config = ConfigDict(extra="ignore")

    firm: str | None = None
    streetAddress: str
    secondaryAddress: str | None = None
    city: str
    state: str
    ZIPCode: str
    ZIPPlus4: str | None = None


class AdditionalInfo(BaseModel):
    """The ``additionalInfo`` sub-object with delivery-point metadata.

    Only a handful of fields are named explicitly; the rest come through via
    ``extra="ignore"`` (USPS returns a moving target of flags like
    ``DPVConfirmation``, ``business``, ``vacant``, etc.).
    """

    model_config = ConfigDict(extra="ignore")

    deliveryPoint: str | None = None
    carrierRoute: str | None = None
    DPVConfirmation: str | None = None
    DPVCMRA: str | None = None
    business: str | None = None
    centralDeliveryPoint: str | None = None
    vacant: str | None = None


class StandardizedAddressResponse(BaseModel):
    """Top-level response from the address-standardization endpoint."""

    model_config = ConfigDict(extra="ignore")

    firm: str | None = None
    address: AddressInfo
    additionalInfo: AdditionalInfo | None = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def full_zip(self) -> str:
        """ZIP+4 if available, otherwise the 5-digit ZIP."""
        if self.address.ZIPPlus4:
            return f"{self.address.ZIPCode}-{self.address.ZIPPlus4}"
        return self.address.ZIPCode


# --------------------------------------------------------------------------- #
# IV-MTR tracking (pull)                                                      #
# --------------------------------------------------------------------------- #


class TrackingScan(BaseModel):
    """One scan event from the IV-MTR tracking endpoint.

    The live IV-MTR pull API returns snake_case keys (``scan_date_time``,
    ``scan_event_code``, ``scan_facility_*``) and carries **no** per-scan
    ``imb`` — the piece IMb appears once at the ``data`` level. Field aliases
    map those wire names onto stable camelCase attribute names, and
    ``populate_by_name`` keeps construction-by-field-name (tests, internal
    code) and ``model_dump_json()`` round-tripping working. ``model_dump_json``
    emits field names, so stored ``event_json`` blobs use the camelCase keys
    that the merge/delivery-gating code reads.
    """

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    scanDatetime: datetime = Field(alias="scan_date_time")
    scanEventCode: str = Field(alias="scan_event_code")
    scanFacilityCity: str | None = Field(default=None, alias="scan_facility_city")
    scanFacilityState: str | None = Field(default=None, alias="scan_facility_state")
    scanFacilityZip: str | None = Field(default=None, alias="scan_facility_zip")
    machineName: str | None = Field(default=None, alias="machine_name")
    mailPhase: str | None = Field(default=None, alias="mail_phase")
    handlingEventType: str | None = Field(default=None, alias="handling_event_type")


class TrackingData(BaseModel):
    """The ``data`` payload on success.

    Carries the piece-level ``imb`` plus the scan list; the many other
    IV-MTR fields (``piece_id``, ``mail_class``, …) are ignored except
    ``expected_delivery_date``, surfaced for ETA display.
    """

    model_config = ConfigDict(extra="ignore")

    imb: str
    scans: list[TrackingScan] = Field(default_factory=list)
    expected_delivery_date: str | None = None


class TrackingResponse(BaseModel):
    """Wrapper returned by the tracking endpoint.

    On success ``data`` is populated and the wire field ``message`` is null.
    On a miss IV-MTR returns ``{"message": "Barcode not found.", "data": null}``
    — ``message`` is aliased onto ``error`` so callers can distinguish "not
    found / error" from a genuinely empty result.
    """

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    data: TrackingData | None = None
    error: str | None = Field(default=None, alias="message")


# --------------------------------------------------------------------------- #
# IV-MTR push-feed webhook                                                    #
# --------------------------------------------------------------------------- #


class PushFeedEvent(BaseModel):
    """One event in an IV-MTR push-feed webhook payload.

    ``handlingEventType`` filtering (we only care about ``"L"`` — letter
    scans — for mailwatch) happens in the route handler, not here. The model
    accepts all event types so we can log the raw push and analyse it later.

    UNVERIFIED AGAINST LIVE DATA: these field names predate any real push
    delivery. The IV-MTR *pull* API was found to return snake_case keys
    (``scan_date_time`` etc.) and ``handling_event_type`` values like ``"A"``
    rather than ``"L"`` — see :class:`TrackingScan`. The push feed likely
    shares that convention, so this model (and the ``handlingEventType == "L"``
    filter in ``routes.post_usps_feed``) should be re-checked against the
    first real captured webhook payload before relying on the push path.
    """

    model_config = ConfigDict(extra="ignore")

    eventId: str
    imb: str
    handlingEventType: str
    scanDatetime: datetime
    scanEventCode: str | None = None
    mailPhase: str | None = None
    machineName: str | None = None
    scannerType: str | None = None
    scanFacilityName: str | None = None
    scanFacilityCity: str | None = None
    scanFacilityState: str | None = None
    scanFacilityZip: str | None = None


class PushFeedPayload(BaseModel):
    """Top-level wrapper for an incoming IV-MTR push-feed delivery."""

    model_config = ConfigDict(extra="ignore")

    events: list[PushFeedEvent] = Field(default_factory=list)
