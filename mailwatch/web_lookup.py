"""Backup address resolver: USPS's free consumer ZIP-lookup via a real browser.

Standby for when the paid/official Addresses API (``apis.usps.com``) is
unavailable. Returns the same :class:`StandardizedAddressResponse` shape the
API does, so a cached entry produced here is indistinguishable to the rest of
mailwatch (see :mod:`mailwatch.cli_resolve`, which seeds ``address_cache``).

Why a browser at all: the data endpoint
``tools.usps.com/.../ziplookup/zipByAddress`` sits behind Akamai Bot Manager.
A live browser running Akamai's JS sensor in-request is mandatory — plain HTTP
clients (including TLS-fingerprint-impersonating ones carrying a
browser-minted ``_abck`` cookie) are blocked on the data XHR. We drive Chromium
via ``nodriver``, headful under a virtual display (``xvfb`` on servers).

``nodriver`` is imported lazily inside the driver so this module — and its pure
:func:`parse_address_list` core — imports without the optional ``browser``
dependency group installed. The engine is isolated to :func:`_capture_lookup`;
swapping it (e.g. for Camoufox) touches nothing else.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
from typing import Any

from mailwatch.models import (
    AdditionalInfo,
    AddressInfo,
    AddressRequest,
    StandardizedAddressResponse,
)

logger = logging.getLogger(__name__)

LOOKUP_URL = "https://tools.usps.com/zip-code-lookup.htm?byaddress"
_DELIVERY_POINT_LEN = 2


class WebLookupError(RuntimeError):
    """The USPS web lookup failed, was blocked by Akamai, or found no match."""


def _norm_dp(value: Any) -> str | None:
    """Keep a delivery point only if it's the expected 2-digit form."""
    if isinstance(value, str) and len(value) == _DELIVERY_POINT_LEN and value.isdigit():
        return value
    return None


def parse_address_list(payload: dict[str, Any]) -> list[StandardizedAddressResponse]:
    """Parse a ``zipByAddress`` JSON body into standardized-response models.

    Pure function — the unit-testable core. Maps USPS's consumer wire fields
    (``addressLine1``/``zip5``/``zip4``/``deliveryPoint``/...) onto the same
    models :class:`~mailwatch.usps_api.NewApiClient` produces. Returns ``[]``
    when the lookup reports anything other than success or carries no usable
    entries (callers treat empty as "no match").
    """
    if payload.get("resultStatus") != "SUCCESS":
        return []

    results: list[StandardizedAddressResponse] = []
    for entry in payload.get("addressList") or []:
        zip5 = entry.get("zip5")
        street = entry.get("addressLine1")
        if not zip5 or not street:
            continue
        firm = entry.get("firm") or None
        address = AddressInfo(
            firm=firm,
            streetAddress=street,
            secondaryAddress=entry.get("addressLine2") or None,
            city=entry.get("city", ""),
            state=entry.get("state", ""),
            ZIPCode=zip5,
            ZIPPlus4=entry.get("zip4") or None,
        )
        additional = AdditionalInfo(
            deliveryPoint=_norm_dp(entry.get("deliveryPoint")),
            carrierRoute=entry.get("carrierRoute") or None,
            DPVConfirmation=entry.get("dpvConfirmation") or None,
        )
        results.append(
            StandardizedAddressResponse(firm=firm, address=address, additionalInfo=additional)
        )
    return results


async def _capture_lookup(  # pragma: no cover - browser I/O, exercised by integration test
    req: AddressRequest,
    *,
    chrome_path: str | None,
    settle_s: float,
) -> dict[str, Any]:
    """Drive Chromium through the lookup form and return the captured JSON.

    Raises :class:`WebLookupError` if Akamai serves the block page or no
    ``zipByAddress`` response is observed.
    """
    # Lazy: nodriver lives in the optional `browser` group, so importing this
    # module (and its pure parser) must not require it.
    import nodriver as uc

    captured: list[dict[str, Any]] = []
    browser = await uc.start(
        headless=False,
        sandbox=False,
        browser_executable_path=chrome_path,
        browser_args=["--window-size=1280,900", "--disable-gpu"],
    )
    try:
        tab = await browser.get(LOOKUP_URL)

        async def on_response(event: Any) -> None:
            if "ziplookup" not in event.response.url.lower():
                return
            with contextlib.suppress(Exception):
                body, _ = await tab.send(uc.cdp.network.get_response_body(event.request_id))
                captured.append(json.loads(body))

        tab.add_handler(uc.cdp.network.ResponseReceived, on_response)
        await tab.send(uc.cdp.network.enable())
        await tab.sleep(3)

        el = await tab.select("#tAddress")
        await el.send_keys(req.streetAddress)
        el = await tab.select("#tCity")
        await el.send_keys(req.city)
        await tab.evaluate(
            "(() => { const s = document.querySelector('#tState');"
            f" if (s) {{ s.value = {req.state!r};"
            " s.dispatchEvent(new Event('change', {bubbles: true})); } })()"
        )
        button = await tab.select("#zip-by-address")
        await button.click()
        await tab.sleep(settle_s)

        body_text = str(await tab.evaluate("document.body.innerText") or "")
    finally:
        browser.stop()

    if captured:
        return captured[-1]
    if "unavailable" in body_text.lower():
        raise WebLookupError("USPS returned the Akamai bot-block page")
    raise WebLookupError("no zipByAddress response was captured")


async def resolve(  # pragma: no cover - thin orchestration over browser I/O
    req: AddressRequest,
    *,
    chrome_path: str | None = None,
    settle_s: float = 7.0,
    retries: int = 1,
) -> list[StandardizedAddressResponse]:
    """Resolve ``req`` against USPS's free web lookup; retry once on a block.

    ``chrome_path`` defaults to ``$CHROME_BIN`` (set by the ``resolve-address``
    flake app to the nix-provided Chromium). Returns the standardized
    candidates (usually one for a deliverable address).
    """
    chrome = chrome_path or os.environ.get("CHROME_BIN")
    last_exc: WebLookupError | None = None
    for attempt in range(retries + 1):
        try:
            payload = await _capture_lookup(req, chrome_path=chrome, settle_s=settle_s)
            return parse_address_list(payload)
        except WebLookupError as exc:
            last_exc = exc
            logger.warning("USPS web lookup attempt %d failed: %s", attempt + 1, exc)
    raise last_exc if last_exc else WebLookupError("lookup failed")
