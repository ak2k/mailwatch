"""Tests for mailwatch.models — USPS API request/response shapes."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from mailwatch.models import (
    AddressRequest,
    IVMTRTokenResponse,
    NewApiTokenResponse,
    OAuthErrorResponse,
    PushFeedEvent,
    PushFeedPayload,
    StandardizedAddressResponse,
    TrackingData,
    TrackingResponse,
    TrackingScan,
)

# --------------------------------------------------------------------------- #
# OAuth token responses                                                       #
# --------------------------------------------------------------------------- #


class TestNewApiTokenResponse:
    def test_parses_realistic_success_payload(self) -> None:
        payload = {
            "access_token": "eyJhbGciOiJSUzI1NiJ9.fake.token",
            "token_type": "Bearer",
            "expires_in": 28799,
            "scope": "addresses tracking",
            "issued_at": "1715020800000",
            "application_name": "mailwatch-dev",
            "api_products": "Addresses, Tracking",
        }
        token = NewApiTokenResponse.model_validate(payload)
        assert token.access_token == "eyJhbGciOiJSUzI1NiJ9.fake.token"
        assert token.token_type == "Bearer"
        assert token.expires_in == 28799
        assert token.scope == "addresses tracking"

    def test_minimal_payload(self) -> None:
        """token_type defaults to Bearer; scope/issued_at optional."""
        token = NewApiTokenResponse.model_validate({"access_token": "abc", "expires_in": 3600})
        assert token.token_type == "Bearer"
        assert token.scope is None

    def test_ignores_unknown_fields(self) -> None:
        token = NewApiTokenResponse.model_validate(
            {
                "access_token": "abc",
                "expires_in": 3600,
                "refresh_token_hint": "future-field",
                "consumer_key": "snooped",
            }
        )
        assert token.access_token == "abc"
        assert not hasattr(token, "refresh_token_hint")


class TestIVMTRTokenResponse:
    def test_parses_with_refresh_token(self) -> None:
        token = IVMTRTokenResponse.model_validate(
            {
                "access_token": "at-abc",
                "refresh_token": "rt-xyz",
                "token_type": "Bearer",
                "expires_in": 86400,
            }
        )
        assert token.refresh_token == "rt-xyz"

    def test_missing_refresh_token_fails(self) -> None:
        with pytest.raises(ValidationError):
            IVMTRTokenResponse.model_validate(
                {"access_token": "at", "token_type": "Bearer", "expires_in": 3600}
            )


class TestOAuthErrorResponse:
    def test_parses_error_shape(self) -> None:
        err = OAuthErrorResponse.model_validate(
            {
                "error": "invalid_client",
                "error_description": "Client authentication failed",
            }
        )
        assert err.error == "invalid_client"
        assert err.error_description == "Client authentication failed"

    def test_description_optional(self) -> None:
        err = OAuthErrorResponse.model_validate({"error": "invalid_grant"})
        assert err.error_description is None


# --------------------------------------------------------------------------- #
# Address standardization                                                     #
# --------------------------------------------------------------------------- #


class TestAddressRequest:
    def _valid_kwargs(self) -> dict[str, str]:
        return {
            "streetAddress": "475 L'Enfant Plaza SW",
            "city": "Washington",
            "state": "DC",
            "ZIPCode": "20260",
        }

    def test_happy_path(self) -> None:
        req = AddressRequest.model_validate(self._valid_kwargs())
        assert req.state == "DC"
        assert req.ZIPCode == "20260"
        assert req.firm is None

    def test_state_must_be_two_chars(self) -> None:
        kwargs = self._valid_kwargs() | {"state": "DCA"}
        with pytest.raises(ValidationError, match="state"):
            AddressRequest.model_validate(kwargs)

    def test_state_must_be_uppercase_letters(self) -> None:
        kwargs = self._valid_kwargs() | {"state": "dc"}
        with pytest.raises(ValidationError):
            AddressRequest.model_validate(kwargs)

    def test_zipcode_must_be_five_digits(self) -> None:
        kwargs = self._valid_kwargs() | {"ZIPCode": "2026"}
        with pytest.raises(ValidationError, match="ZIPCode"):
            AddressRequest.model_validate(kwargs)

    def test_zipcode_rejects_letters(self) -> None:
        kwargs = self._valid_kwargs() | {"ZIPCode": "2026A"}
        with pytest.raises(ValidationError):
            AddressRequest.model_validate(kwargs)

    def test_zipplus4_optional_but_validated(self) -> None:
        kwargs = self._valid_kwargs() | {"ZIPPlus4": "1234"}
        req = AddressRequest.model_validate(kwargs)
        assert req.ZIPPlus4 == "1234"

        bad = self._valid_kwargs() | {"ZIPPlus4": "12"}
        with pytest.raises(ValidationError):
            AddressRequest.model_validate(bad)


class TestStandardizedAddressResponse:
    def test_full_zip_with_plus4(self) -> None:
        resp = StandardizedAddressResponse.model_validate(
            {
                "address": {
                    "streetAddress": "475 L'Enfant Plaza SW",
                    "city": "Washington",
                    "state": "DC",
                    "ZIPCode": "20260",
                    "ZIPPlus4": "0004",
                },
            }
        )
        assert resp.full_zip == "20260-0004"

    def test_full_zip_without_plus4(self) -> None:
        resp = StandardizedAddressResponse.model_validate(
            {
                "address": {
                    "streetAddress": "475 L'Enfant Plaza SW",
                    "city": "Washington",
                    "state": "DC",
                    "ZIPCode": "20260",
                },
            }
        )
        assert resp.full_zip == "20260"

    def test_additional_info_and_unknown_fields_ignored(self) -> None:
        resp = StandardizedAddressResponse.model_validate(
            {
                "firm": "USPS HQ",
                "address": {
                    "streetAddress": "475 L'Enfant Plaza SW",
                    "city": "Washington",
                    "state": "DC",
                    "ZIPCode": "20260",
                    "ZIPPlus4": "0004",
                    "unknown_future_field": "ignored",
                },
                "additionalInfo": {
                    "deliveryPoint": "75",
                    "carrierRoute": "C001",
                    "DPVConfirmation": "Y",
                    "some_new_usps_flag": "Z",
                },
                "corrections": [{"code": "A1000"}],
                "matches": [{"code": "1"}],
            }
        )
        assert resp.firm == "USPS HQ"
        assert resp.additionalInfo is not None
        assert resp.additionalInfo.deliveryPoint == "75"
        assert resp.full_zip == "20260-0004"


# --------------------------------------------------------------------------- #
# IV-MTR tracking                                                             #
# --------------------------------------------------------------------------- #


class TestTrackingResponse:
    def test_success_with_data(self) -> None:
        resp = TrackingResponse.model_validate(
            {
                "data": {
                    "imb": "0112345678901234567890123456789012345678901234567890",
                    "scans": [
                        {
                            "imb": "0112345678901234567890123456789012345678901234567890",
                            "scanDatetime": "2026-04-20T14:32:11Z",
                            "scanEventCode": "SPM",
                            "scanFacilityCity": "NEW YORK",
                            "scanFacilityState": "NY",
                            "scanFacilityZip": "10001",
                            "machineName": "APBS-03",
                        },
                        {
                            "imb": "0112345678901234567890123456789012345678901234567890",
                            "scanDatetime": "2026-04-20T22:10:00Z",
                            "scanEventCode": "DEL",
                            "extra_new_field": "tolerated",
                        },
                    ],
                }
            }
        )
        assert resp.error is None
        assert resp.data is not None
        assert len(resp.data.scans) == 2
        assert resp.data.scans[0].scanEventCode == "SPM"
        assert resp.data.scans[0].scanDatetime == datetime(2026, 4, 20, 14, 32, 11, tzinfo=UTC)
        assert resp.data.scans[1].scanFacilityCity is None

    def test_error_payload(self) -> None:
        resp = TrackingResponse.model_validate({"error": "IMB not found in IV-MTR index"})
        assert resp.data is None
        assert resp.error == "IMB not found in IV-MTR index"

    def test_empty_scans_default(self) -> None:
        td = TrackingData.model_validate(
            {"imb": "0112345678901234567890123456789012345678901234567890"}
        )
        assert td.scans == []

    def test_scan_rejects_missing_required(self) -> None:
        with pytest.raises(ValidationError):
            TrackingScan.model_validate(
                {"scanDatetime": "2026-04-20T00:00:00Z", "scanEventCode": "SPM"}
            )


# --------------------------------------------------------------------------- #
# Push-feed events                                                            #
# --------------------------------------------------------------------------- #


class TestPushFeedPayload:
    def test_parses_mixed_handling_event_types(self) -> None:
        payload = {
            "events": [
                {
                    "eventId": "evt-001",
                    "imb": "0112345678901234567890123456789012345678901234567890",
                    "handlingEventType": "L",
                    "scanDatetime": "2026-04-20T14:32:11Z",
                    "scanEventCode": "SPM",
                    "scanFacilityCity": "NEW YORK",
                    "scanFacilityState": "NY",
                    "scanFacilityZip": "10001",
                },
                {
                    "eventId": "evt-002",
                    "imb": "0199999999999999999999999999999999999999999999999999",
                    "handlingEventType": "F",  # flat — we filter elsewhere
                    "scanDatetime": "2026-04-20T15:00:00Z",
                    "scanEventCode": "PRO",
                    "mailPhase": "Phase 1",
                },
                {
                    "eventId": "evt-003",
                    "imb": "0112345678901234567890123456789012345678901234567890",
                    "handlingEventType": "L",
                    "scanDatetime": "2026-04-21T08:00:00Z",
                    "machineName": "APBS-14",
                    "scannerType": "AFCS",
                    "some_future_field": "ignored",
                },
            ]
        }
        parsed = PushFeedPayload.model_validate(payload)
        assert len(parsed.events) == 3
        assert [e.handlingEventType for e in parsed.events] == ["L", "F", "L"]
        assert parsed.events[2].machineName == "APBS-14"

    def test_empty_events_default(self) -> None:
        parsed = PushFeedPayload.model_validate({})
        assert parsed.events == []


class TestPushFeedEvent:
    def _valid_kwargs(self) -> dict[str, str]:
        return {
            "eventId": "evt-001",
            "imb": "0112345678901234567890123456789012345678901234567890",
            "handlingEventType": "L",
            "scanDatetime": "2026-04-20T14:32:11Z",
        }

    def test_happy_path(self) -> None:
        ev = PushFeedEvent.model_validate(self._valid_kwargs())
        assert ev.eventId == "evt-001"
        assert ev.scanEventCode is None

    @pytest.mark.parametrize(
        "missing_field",
        ["eventId", "imb", "handlingEventType", "scanDatetime"],
    )
    def test_missing_required_raises(self, missing_field: str) -> None:
        kwargs = self._valid_kwargs()
        del kwargs[missing_field]
        with pytest.raises(ValidationError) as exc_info:
            PushFeedEvent.model_validate(kwargs)
        assert missing_field in str(exc_info.value)

    def test_extra_fields_ignored(self) -> None:
        kwargs = self._valid_kwargs() | {
            "brandNewUspsField": "should-be-ignored",
            "anotherOne": 42,
        }
        ev = PushFeedEvent.model_validate(kwargs)
        assert not hasattr(ev, "brandNewUspsField")
        assert not hasattr(ev, "anotherOne")
