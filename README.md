# mailwatch

Self-hosted USPS Intelligent Mail barcode generator and letter tracker. Generate envelope or Avery-label PDFs with a scannable IMb, then watch USPS scan events as your letters move through the sort network.

## What it does

1. Renders printable envelopes (#10, 9.5" × 4.125") and 2×5 label sheets (Avery 8163) with a valid USPS IMb
2. Queries USPS IV-MTR for scan events on your generated barcodes
3. Receives push webhooks from USPS IV-MTR for real-time tracking updates
4. Stores scan history in SQLite, replicated via Litestream if configured

## Requirements

- Python 3.12+
- A USPS Business Customer Gateway (BCG) account with:
  - A Mailer ID (MID)
  - Informed Visibility – Mail Tracking & Reporting (IV-MTR) access
- A USPS developer API OAuth client (https://developer.usps.com)
- Optional: publicly reachable HTTPS endpoint for the IV-MTR push feed

## Quick start (development)

The canonical dev workflow is nix-driven — `nix run .#dev` and `nix run .#test`
set the WeasyPrint native-library path correctly for both linux
(`LD_LIBRARY_PATH`) and darwin (`DYLD_FALLBACK_LIBRARY_PATH` — SIP strips
`LD_LIBRARY_PATH` on macOS):

```sh
cp .env.example .env
# Fill in real values in .env
nix run .#dev                         # uvicorn reload-server
nix run .#test                        # pytest
nix run .#dev -- --port 5000          # extra args pass through
nix run .#test -- -k envelope -vv
```

If you prefer uv directly, `nix develop` drops you into a shell with
`UV_PYTHON` + the library path pre-set, after which:

```sh
uv run uvicorn --factory mailwatch.app:create_app --reload
uv run pytest
```

Open http://127.0.0.1:8000 to generate an envelope.

## Running the test suite

Canonical (matches CI):

```sh
nix flake check -L                    # lint + typecheck + tests + nix hygiene
nix build .#checks.$SYSTEM.tests      # just pytest, any $SYSTEM
```

Fast iteration:

```sh
nix run .#test                        # pytest with libs pre-wired
uv run pytest                         # inside `nix develop`
uv run ruff check .
uv run ruff format --check .
uv run mypy mailwatch
```

## Production deployment

### NixOS (recommended)

Consume this flake as an input:

```nix
{
  inputs.mailwatch.url = "github:ak2k/mailwatch";
  # In your NixOS configuration:
  imports = [ inputs.mailwatch.nixosModules.mailwatch ];
  services.mailwatch = {
    enable = true;
    domain = "mail.example.com";
    environmentFile = "/run/secrets/mailwatch.env";  # sops-nix, agenix, etc.
  };
}
```

### Other deployments

- Run `uv run gunicorn 'mailwatch.app:create_app()' --workers 2 --worker-class uvicorn.workers.UvicornWorker --bind 127.0.0.1:8082` (factory syntax — the module exposes `create_app`, not a singleton `app`)
- Front with a reverse proxy that terminates TLS and passes `X-Forwarded-For`
- Gate most routes behind your auth layer; **exclude `/usps_feed`** — USPS must be able to POST to it by source IP only

## Configuration

All configuration is read from environment variables (see `.env.example`). Secrets must never be committed.

| Variable | Purpose |
|---|---|
| `MAILER_ID` | USPS-assigned Mailer ID (6 or 9 digits) |
| `SRV_TYPE` | IMb Service Type (40 = First-Class single-piece full-service) |
| `BARCODE_ID` | 2-digit barcode ID (0 unless routing) |
| `BSG_USERNAME` / `BSG_PASSWORD` | USPS Business Customer Gateway credentials (IV-MTR auth) |
| `USPS_NEWAPI_CUSTOMER_ID` / `_SECRET` | developer.usps.com OAuth client |
| `SESSION_KEY` | 32-byte hex string for session cookie signing |
| `DB_PATH` | SQLite file path |
| `RATE_LIMIT_PER_HOUR` | Client-side cap on USPS API calls |
| `USPS_FEED_CIDRS` | Comma-separated CIDRs allowed to POST `/usps_feed` |

## Rate limits

USPS's default tier for new developer accounts is **60 req/hr per application** across all endpoints on `apis.usps.com` (addresses + tracking + labels). Request a tier upgrade to 300 req/hr by emailing `emailus.usps.com` with your CRID, app name, and mailer-use justification. IV-MTR tracking calls (`iv.usps.com`) are on a separate bucket.

## License

MIT. See [LICENSE](LICENSE).

## Acknowledgements

Ported from [`1997cui/envelope`](https://github.com/1997cui/envelope) (MIT). IMb algorithm is USPS-B-3200.
