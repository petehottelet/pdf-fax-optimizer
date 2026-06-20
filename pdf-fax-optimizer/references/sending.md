# Sending a fax via a cloud API

Once a file is fax-optimized (1-bit CCITT-G4 PDF/TIFF), you can transmit it with
a cloud fax API instead of a physical machine. The provider runs the analog/T.38
side, so you need only an API key and the recipient number in **E.164** format
(e.g. `+14155551234`). Use `pdf-fax-send` (the installed console script, or
`python3 -m pdf_fax_optimizer.send_fax`), or `pdf-fax-optimizer ... --send` to
optimize and transmit in one step.

> **Always pass credentials via environment variables**, not on the command
> line (shell history leaks keys). And **`--dry-run` first** to confirm the exact
> request — it prints what would be sent and needs no credentials.

## Built-in providers (verified request shapes)

### mFax / Documo  (`--provider mfax`, the default)

- `POST https://api.documo.com/v1/fax/send`
- Auth: header `Authorization: Basic <API_KEY>` (the raw API key)
- Fields: `recipientFax`, `file` (+ optional `coverPage`, `recipientName`,
  `subject`, `notes`)
- Env: `MFAX_API_KEY`

```bash
export MFAX_API_KEY=sk_live_xxx
python3 -m pdf_fax_optimizer.send_fax output.fax.pdf --provider mfax --to +14155551234
# with a cover page:
python3 -m pdf_fax_optimizer.send_fax output.fax.pdf --provider mfax --to +14155551234 \
    --cover-page --recipient-name "Dr. Lee" --subject "Intake" --notes "2 pages"
```

### Phaxio / Sinch  (`--provider phaxio`)

- `POST https://api.phaxio.com/v2.1/faxes`
- Auth: HTTP basic `API_KEY:API_SECRET`
- Fields: `to`, `file` (+ optional `caller_id`)
- Env: `PHAXIO_API_KEY`, `PHAXIO_API_SECRET`

```bash
export PHAXIO_API_KEY=xxx PHAXIO_API_SECRET=yyy
python3 -m pdf_fax_optimizer.send_fax output.fax.pdf --provider phaxio --to +14155551234
```

## Any other provider  (`--provider generic`)

A configurable multipart sender for any API that accepts a file upload. Set the
endpoint, auth, and field names to match the provider's docs.

```bash
# Bearer-token API that takes `fax_number` + `document` multipart fields:
python3 -m pdf_fax_optimizer.send_fax output.fax.pdf --provider generic \
    --url https://api.example.com/v1/faxes \
    --auth-header "Bearer $FAX_API_KEY" \
    --to-field fax_number --file-field document \
    --to +14155551234 --field quality=fine
```

- `--auth-header "Bearer KEY"` **or** `--basic-user`/`--basic-pass` for HTTP basic
- `--to-field` / `--file-field` rename the recipient and file parts
- `--field k=v` (repeatable) adds extra multipart fields
- `FAX_API_KEY` (env) is used as `Bearer` auth if no auth flag is given

### Notes for specific providers via `generic`

- **Telnyx** (`https://api.telnyx.com/v2/faxes`, `Authorization: Bearer …`)
  expects JSON with `connection_id`, `from`, `to`, and a **publicly reachable
  `media_url`** rather than a direct upload — host the optimized file first and
  pass its URL. The multipart `generic` path suits providers that accept uploads.
- **SRFax / others** — match their field names with `--to-field`/`--file-field`
  and `--field`.

## Optimize and send in one step

```bash
export MFAX_API_KEY=sk_live_xxx
python3 -m pdf_fax_optimizer.optimize_pdf INPUT.pdf -o OUTPUT.fax.pdf \
    --fax-resolution fine --dither auto \
    --send mfax --to +14155551234 --dry-run     # drop --dry-run to transmit
```

`pdf-fax-optimizer` accepts the same sending flags as `pdf-fax-send`. The file is
optimized and the report printed first, then the transmission is attempted.

## Result & exit code

Both tools print a JSON result (`ok`, `status_code`, and the provider's response
body, which includes the fax id/`uuid` and queued `status`). Exit code is non-zero
on failure so scripts can branch. Poll the provider's status endpoint or register
a status webhook (see your provider's docs) to confirm final delivery —
`status` at submit time only means *queued*, not *delivered*.
```
