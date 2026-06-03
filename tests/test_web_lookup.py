"""Tests for the backup browser-driven address resolver.

The browser/CDP layer (`web_lookup._capture_lookup`, `web_lookup.resolve`) is
exercised only by the network-marked integration test below; the default suite
covers the pure parser and the cache-seam contract that lets a resolver-seeded
entry be served by the unchanged `/generate` path.
"""

from __future__ import annotations

import argparse

import pytest

from mailwatch import db
from mailwatch.cli_resolve import _build_request
from mailwatch.models import AddressRequest, StandardizedAddressResponse
from mailwatch.web_lookup import parse_address_list

# A real `zipByAddress` body captured from tools.usps.com (475 L'Enfant Plaza).
SAMPLE_PAYLOAD = {
    "resultStatus": "SUCCESS",
    "addressList": [
        {
            "addressLine1": "475 LENFANT PLZ SW",
            "city": "WASHINGTON",
            "state": "DC",
            "zip5": "20260",
            "zip4": "0001",
            "carrierRoute": "C000",
            "countyName": "DISTRICT OF COLUMBIA",
            "deliveryPoint": "75",
            "checkDigit": "7",
            "dpvConfirmation": "Y",
        }
    ],
}


def test_parse_maps_all_fields() -> None:
    [std] = parse_address_list(SAMPLE_PAYLOAD)
    assert std.address.streetAddress == "475 LENFANT PLZ SW"
    assert std.address.city == "WASHINGTON"
    assert std.address.state == "DC"
    assert std.address.ZIPCode == "20260"
    assert std.address.ZIPPlus4 == "0001"
    assert std.full_zip == "20260-0001"
    assert std.additionalInfo is not None
    assert std.additionalInfo.deliveryPoint == "75"
    assert std.additionalInfo.carrierRoute == "C000"
    assert std.additionalInfo.DPVConfirmation == "Y"


def test_parse_non_success_returns_empty() -> None:
    assert parse_address_list({"resultStatus": "ADDRESS_NOT_FOUND", "addressList": []}) == []
    assert parse_address_list({}) == []


def test_parse_skips_entries_without_zip_or_street() -> None:
    payload = {
        "resultStatus": "SUCCESS",
        "addressList": [
            {"addressLine1": "1 NOWHERE ST", "city": "X", "state": "DC"},  # no zip5
            {"zip5": "20260", "city": "X", "state": "DC"},  # no street
        ],
    }
    assert parse_address_list(payload) == []


def test_parse_drops_malformed_delivery_point() -> None:
    payload = {
        "resultStatus": "SUCCESS",
        "addressList": [{**SAMPLE_PAYLOAD["addressList"][0], "deliveryPoint": "7"}],
    }
    [std] = parse_address_list(payload)
    assert std.additionalInfo is not None
    assert std.additionalInfo.deliveryPoint is None  # 1 digit -> not kept


def test_seeded_value_round_trips_as_service_reads_it() -> None:
    """The stored cache value must be consumable by the unchanged service path,
    which does ``StandardizedAddressResponse.model_validate_json(cached)``."""
    [std] = parse_address_list(SAMPLE_PAYLOAD)
    stored = std.model_dump_json()
    reloaded = StandardizedAddressResponse.model_validate_json(stored)
    assert reloaded.full_zip == "20260-0001"
    assert reloaded.additionalInfo is not None
    assert reloaded.additionalInfo.deliveryPoint == "75"


def _args(**kw: str | None) -> argparse.Namespace:
    base: dict[str, str | None] = {
        "street": "475 L'Enfant Plaza SW",
        "city": "Washington",
        "state": "DC",
        "zip": "20260",
        "company": None,
        "address2": None,
    }
    base.update(kw)
    return argparse.Namespace(**base)


def test_cli_cache_key_matches_service_construction() -> None:
    """The CLI's request must hash to the key the service computes, or a seeded
    entry is never found. The service builds the request exactly as below
    (routes._standardize_for_generate) and keys on
    ``model_dump(exclude_none=True)``."""
    cli_req = _build_request(_args())
    service_req = AddressRequest(
        firm=None,
        streetAddress="475 L'Enfant Plaza SW",
        secondaryAddress=None,
        city="Washington",
        state="DC",
        ZIPCode="20260",
    )
    cli_key = db.hash_address_dict(cli_req.model_dump(exclude_none=True))
    service_key = db.hash_address_dict(service_req.model_dump(exclude_none=True))
    assert cli_key == service_key


def test_cli_zip_is_cleaned_to_five_digits() -> None:
    req = _build_request(_args(zip="20260-0001"))
    assert req.ZIPCode == "20260"


def test_cli_optional_fields_change_the_key() -> None:
    """firm/secondaryAddress presence is part of the key — they must be supplied
    iff they were entered on /generate."""
    bare = db.hash_address_dict(_build_request(_args()).model_dump(exclude_none=True))
    with_company = db.hash_address_dict(
        _build_request(_args(company="ACME")).model_dump(exclude_none=True)
    )
    assert bare != with_company


@pytest.mark.integration
async def test_live_resolve_beats_akamai() -> None:
    """Live canary — run with ``-m integration`` on a host with Chromium+xvfb
    (or CHROME_BIN set). Confirms the resolver still defeats USPS's Akamai."""
    pytest.importorskip("nodriver")
    from mailwatch.web_lookup import resolve

    req = AddressRequest(
        streetAddress="475 L'Enfant Plaza SW", city="Washington", state="DC", ZIPCode="20260"
    )
    results = await resolve(req)
    assert results, "no results — Akamai may have blocked, or selectors drifted"
    assert results[0].address.ZIPCode == "20260"
    assert results[0].additionalInfo is not None
    assert results[0].additionalInfo.deliveryPoint is not None
