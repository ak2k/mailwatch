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
    optional ones as ``str | None`` so callers that want them can read them,
    but the happy path only needs the first three.
    """

    model_config = ConfigDict(extra="ignore")

    access_token: str
    token_type: str = "Bearer"
    expires_in: int
    scope: str | None = None
    issued_at: str | None = None
    application_name: str | None = None
    api_products: str | None = None


class IVMTRTokenResponse(BaseModel):
    """Response from ``POST services.usps.com/oauth/authenticate`` (IV-MTR).

    Legacy OAuth flow: unlike the modern apis.usps.com endpoint this one also
    returns a refresh_token.
    """

    model_config = ConfigDict(extra="ignore")

    access_token: str
    refresh_token: str
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
    """One scan event from the IV-MTR tracking endpoint."""

    model_config = ConfigDict(extra="ignore")

    imb: str
    scanDatetime: datetime
    scanEventCode: str
    scanFacilityCity: str | None = None
    scanFacilityState: str | None = None
    scanFacilityZip: str | None = None
    machineName: str | None = None


class TrackingData(BaseModel):
    """The ``data`` payload on success."""

    model_config = ConfigDict(extra="ignore")

    imb: str
    scans: list[TrackingScan] = Field(default_factory=list)


class TrackingResponse(BaseModel):
    """Wrapper returned by the tracking endpoint.

    On success ``data`` is populated; on error ``error`` holds a message.
    Both can technically be present (USPS occasionally returns partial
    results with a warning), so we don't mark them mutually exclusive.
    """

    model_config = ConfigDict(extra="ignore")

    data: TrackingData | None = None
    error: str | None = None


# --------------------------------------------------------------------------- #
# IV-MTR push-feed webhook                                                    #
# --------------------------------------------------------------------------- #


class PushFeedEvent(BaseModel):
    """One event in an IV-MTR push-feed webhook payload.

    ``handlingEventType`` filtering (we only care about ``"L"`` — letter
    scans — for mailwatch) happens in the route handler, not here. The model
    accepts all event types so we can log the raw push and analyse it later.
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
