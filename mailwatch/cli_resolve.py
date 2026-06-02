"""Standby CLI: resolve a USPS address via the backup browser lookup and seed
``address_cache`` so mailwatch's ``/generate`` path serves it without the API.

Use when ``apis.usps.com`` is unavailable. Pass the **same recipient fields you
enter on the generate form** — the cache key is the hash of that raw input
(``mailwatch.usps_api.NewApiClient.validate_address``), so the fields must match
for the service to get a hit. The cached value is a standardized response
identical in shape to the API's, so nothing else in mailwatch changes.

    python -m mailwatch.cli_resolve "475 L'Enfant Plaza SW" \
        --city Washington --state DC --zip 20260

Runs Chromium headful; on a display-less server wrap it in ``xvfb-run`` (the
``resolve-address`` flake app does this and points it at the nix Chromium).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from mailwatch import db
from mailwatch.config import get_settings
from mailwatch.models import AddressRequest, StandardizedAddressResponse
from mailwatch.web_lookup import WebLookupError, resolve

_ZIP5_LEN = 5


def _clean_zip(raw: str) -> str:
    """Digits-only, first 5 — mirrors ``routes._clean_zip(...)[:5]`` so the
    cache key matches what ``/generate`` computes for the same input."""
    return "".join(ch for ch in raw if ch.isdigit())[:_ZIP5_LEN]


def _build_request(args: argparse.Namespace) -> AddressRequest:
    """Construct the request identically to ``routes._standardize_for_generate``
    so ``hash_address_dict`` produces the same cache key the service looks up."""
    return AddressRequest(
        firm=args.company or None,
        streetAddress=args.street,
        secondaryAddress=args.address2 or None,
        city=args.city,
        state=args.state.upper(),
        ZIPCode=_clean_zip(args.zip),
    )


def _format_human(std: StandardizedAddressResponse) -> str:
    dp = std.additionalInfo.deliveryPoint if std.additionalInfo else None
    line2 = f"{std.address.city} {std.address.state} {std.full_zip}"
    return (
        f"  {std.address.streetAddress}\n"
        f"  {line2}\n"
        f"  ZIP+4: {std.full_zip}   delivery point: {dp or '(none)'}"
    )


async def _run(args: argparse.Namespace) -> int:
    req = _build_request(args)
    try:
        candidates = await resolve(req, chrome_path=args.chrome)
    except WebLookupError as exc:
        print(f"lookup failed: {exc}", file=sys.stderr)
        return 2

    if not candidates:
        print("no USPS match for that address", file=sys.stderr)
        return 1
    if len(candidates) > 1:
        print(f"warning: {len(candidates)} candidates returned; using the first", file=sys.stderr)

    chosen = candidates[0]
    if args.json:
        print(chosen.model_dump_json(indent=2))
    else:
        print(_format_human(chosen))

    if args.print_only:
        return 0

    db_path = args.db or get_settings().DB_PATH
    conn = db.connect(Path(db_path))
    try:
        db.init_db(conn)
        key = db.hash_address_dict(req.model_dump(exclude_none=True))
        db.cache_put(conn, key, chosen.model_dump_json())
    finally:
        conn.close()
    print(f"seeded address_cache ({key[:12]}…) in {db_path}", file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="mailwatch.cli_resolve",
        description="Backup USPS address resolver — seeds mailwatch's address_cache.",
    )
    parser.add_argument("street", help="Street address line (as entered on /generate)")
    parser.add_argument("--city", required=True)
    parser.add_argument("--state", required=True, help="2-letter state code")
    parser.add_argument("--zip", required=True, help="5-digit ZIP (as entered on /generate)")
    parser.add_argument("--company", default=None, help="Firm/company line, if any")
    parser.add_argument("--address2", default=None, help="Secondary line (apt/suite), if any")
    parser.add_argument("--db", default=None, help="SQLite path (default: settings DB_PATH)")
    parser.add_argument("--chrome", default=None, help="Chromium binary (default: $CHROME_BIN)")
    parser.add_argument(
        "--json", action="store_true", help="Emit the standardized response as JSON"
    )
    parser.add_argument(
        "--print-only", action="store_true", help="Resolve and print without writing the cache"
    )
    args = parser.parse_args(argv)
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
