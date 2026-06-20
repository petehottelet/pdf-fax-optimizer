#!/usr/bin/env python3
"""Send a (fax-optimized) file through a cloud fax API.

This is the transmission stage that pairs with the optimizer: once a page is a
fax-native 1-bit CCITT-G4 PDF/TIFF, hand it to a fax provider's REST API instead
of a physical machine. Providers run the analog/T.38 side in the cloud, so no
modem or phone line is needed — just an API key and a recipient number in E.164.

Built-in providers (exact, verified request shapes):

  mfax    POST https://api.documo.com/v1/fax/send      (mFax / Documo)
          header  Authorization: Basic <API_KEY>
          fields  recipientFax, file [, coverPage, recipientName, subject, notes]

  phaxio  POST https://api.phaxio.com/v2.1/faxes        (Phaxio / Sinch)
          HTTP basic auth  API_KEY:API_SECRET
          fields  to, file [, caller_id]

  generic POST <your --url>                             (Telnyx, Sinch, SRFax, …)
          configurable auth + field names; see references/sending.md

Credentials come from flags or environment variables (preferred — never put a
key on the command line in shared history):

  mfax     MFAX_API_KEY            (or --api-key)
  phaxio   PHAXIO_API_KEY / PHAXIO_API_SECRET
  generic  FAX_API_KEY            (used as --auth-header bearer if none given)

Use --dry-run to print exactly what would be sent without transmitting (and
without needing credentials).
"""
from __future__ import annotations

import argparse
import json
import mimetypes
import os
import sys


def _mime(path: str) -> str:
    return mimetypes.guess_type(path)[0] or "application/pdf"


def _result(resp) -> dict:
    ok = 200 <= resp.status_code < 300
    try:
        body = resp.json()
    except Exception:
        body = {"raw": resp.text[:2000]}
    return {"ok": ok, "status_code": resp.status_code, "response": body}


def _require_requests():
    try:
        import requests  # noqa: F401
        return requests
    except ImportError:
        sys.exit("The 'requests' package is required to send faxes. Install it "
                 'with: pip install "pdf-fax-optimizer[send]"  (or pip install '
                 "requests)")


# --------------------------------------------------------------------------- #
# Providers                                                                   #
# --------------------------------------------------------------------------- #
def send_mfax(path, to, *, api_key, base_url=None, cover_page=False,
              recipient_name=None, subject=None, notes=None,
              dry_run=False) -> dict:
    url = base_url or "https://api.documo.com/v1/fax/send"
    headers = {"Authorization": f"Basic {api_key}"}
    data = {"recipientFax": to}
    if cover_page:
        data["coverPage"] = "true"
    if recipient_name:
        data["recipientName"] = recipient_name
    if subject:
        data["subject"] = subject
    if notes:
        data["notes"] = notes
    if dry_run:
        return _dry("mfax", url, "header:Authorization Basic ***", data, path)
    requests = _require_requests()
    with open(path, "rb") as fh:
        files = {"file": (os.path.basename(path), fh, _mime(path))}
        resp = requests.post(url, headers=headers, data=data, files=files,
                             timeout=180)
    return _result(resp)


def send_phaxio(path, to, *, api_key, api_secret, base_url=None,
                caller_id=None, dry_run=False) -> dict:
    url = base_url or "https://api.phaxio.com/v2.1/faxes"
    data = {"to": to}
    if caller_id:
        data["caller_id"] = caller_id
    if dry_run:
        return _dry("phaxio", url, "basic:API_KEY:***", data, path)
    requests = _require_requests()
    with open(path, "rb") as fh:
        files = {"file": (os.path.basename(path), fh, _mime(path))}
        resp = requests.post(url, auth=(api_key, api_secret), data=data,
                             files=files, timeout=180)
    return _result(resp)


def send_generic(path, to, *, url, auth_header=None, basic_user=None,
                 basic_pass=None, to_field="to", file_field="file",
                 extra=None, dry_run=False) -> dict:
    """Configurable multipart sender for any provider whose API accepts a file
    upload (Telnyx via media, Sinch, SRFax, etc.). Auth is either a literal
    Authorization header (`--auth-header "Bearer xyz"`) or HTTP basic
    (`--basic-user`/`--basic-pass`)."""
    if not url:
        sys.exit("generic provider requires --url")
    headers = {}
    auth = None
    if auth_header:
        headers["Authorization"] = auth_header
    if basic_user is not None:
        auth = (basic_user, basic_pass or "")
    data = {to_field: to}
    if extra:
        data.update(extra)
    if dry_run:
        a = "header:" + (auth_header[:12] + "***" if auth_header else "")
        if auth:
            a = "basic:" + basic_user + ":***"
        return _dry("generic", url, a, data, path, file_field=file_field)
    requests = _require_requests()
    with open(path, "rb") as fh:
        files = {file_field: (os.path.basename(path), fh, _mime(path))}
        resp = requests.post(url, headers=headers, auth=auth, data=data,
                             files=files, timeout=180)
    return _result(resp)


def _dry(provider, url, auth, data, path, file_field="file") -> dict:
    return {
        "ok": True,
        "dry_run": True,
        "provider": provider,
        "request": {
            "method": "POST",
            "url": url,
            "auth": auth,
            "fields": data,
            "files": {file_field: f"{os.path.basename(path)} ({_mime(path)}, "
                                  f"{os.path.getsize(path)} bytes)"},
        },
    }


# --------------------------------------------------------------------------- #
# Dispatch + CLI                                                              #
# --------------------------------------------------------------------------- #
def send(provider, path, to, args) -> dict:
    if not os.path.exists(path):
        sys.exit(f"file not found: {path}")
    if provider == "mfax":
        key = args.api_key or os.environ.get("MFAX_API_KEY")
        if not key and not args.dry_run:
            sys.exit("mfax needs --api-key or MFAX_API_KEY")
        return send_mfax(path, to, api_key=key or "DRY", base_url=args.base_url,
                         cover_page=args.cover_page,
                         recipient_name=args.recipient_name,
                         subject=args.subject, notes=args.notes,
                         dry_run=args.dry_run)
    if provider == "phaxio":
        key = args.api_key or os.environ.get("PHAXIO_API_KEY")
        sec = args.api_secret or os.environ.get("PHAXIO_API_SECRET")
        if (not key or not sec) and not args.dry_run:
            sys.exit("phaxio needs --api-key/--api-secret or "
                     "PHAXIO_API_KEY/PHAXIO_API_SECRET")
        return send_phaxio(path, to, api_key=key or "DRY",
                           api_secret=sec or "DRY", base_url=args.base_url,
                           caller_id=args.caller_id, dry_run=args.dry_run)
    # generic
    extra = {}
    for kv in args.field or []:
        k, _, v = kv.partition("=")
        extra[k] = v
    auth_header = args.auth_header
    if not auth_header and not args.basic_user:
        env = os.environ.get("FAX_API_KEY")
        if env:
            auth_header = f"Bearer {env}"
    return send_generic(path, to, url=args.url, auth_header=auth_header,
                        basic_user=args.basic_user, basic_pass=args.basic_pass,
                        to_field=args.to_field, file_field=args.file_field,
                        extra=extra, dry_run=args.dry_run)


def main():
    p = argparse.ArgumentParser(description="Send a fax-optimized file via a "
                                            "cloud fax API")
    p.add_argument("file", help="the (optimized) PDF/TIFF to fax")
    p.add_argument("--provider", choices=["mfax", "phaxio", "generic"],
                   default="mfax")
    p.add_argument("--to", required=True, help="recipient fax number in E.164")
    p.add_argument("--api-key")
    p.add_argument("--api-secret")
    p.add_argument("--base-url", help="override the provider endpoint")
    p.add_argument("--dry-run", action="store_true",
                   help="print the request that would be sent; do not transmit")
    # mfax cover-page extras
    p.add_argument("--cover-page", action="store_true")
    p.add_argument("--recipient-name")
    p.add_argument("--subject")
    p.add_argument("--notes")
    # phaxio
    p.add_argument("--caller-id")
    # generic
    p.add_argument("--url")
    p.add_argument("--auth-header", help='e.g. "Bearer KEY"')
    p.add_argument("--basic-user")
    p.add_argument("--basic-pass")
    p.add_argument("--to-field", default="to")
    p.add_argument("--file-field", default="file")
    p.add_argument("--field", action="append",
                   help="extra multipart field k=v (repeatable)")
    args = p.parse_args()

    result = send(args.provider, args.file, args.to, args)
    print(json.dumps(result, indent=2))
    sys.exit(0 if result.get("ok") else 1)


if __name__ == "__main__":
    main()
