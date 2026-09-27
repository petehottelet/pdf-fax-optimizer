"""Sending: --dry-run for every provider builds the expected request payload and
makes no HTTP call."""
from __future__ import annotations

import pytest

from pdf_fax_optimizer import send_fax


@pytest.fixture
def fake_pdf(tmp_path):
    p = tmp_path / "out.fax.pdf"
    p.write_bytes(b"%PDF-1.4\n% fake fax payload\n")
    return str(p)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Any real HTTP attempt should fail the test loudly."""
    def _boom(*a, **k):  # pragma: no cover - only hit on regression
        raise AssertionError("send_fax made a network call during --dry-run")

    import sys
    import types

    fake_requests = types.ModuleType("requests")
    fake_requests.post = _boom
    monkeypatch.setitem(sys.modules, "requests", fake_requests)


def test_mfax_dry_run(fake_pdf):
    res = send_fax.send_mfax(fake_pdf, "+14155551234", api_key="DRY",
                             dry_run=True)
    assert res["ok"] and res["dry_run"]
    assert res["provider"] == "mfax"
    req = res["request"]
    assert req["method"] == "POST"
    assert req["url"].endswith("/v1/fax/send")
    assert req["fields"]["recipientFax"] == "+14155551234"
    assert "file" in req["files"]


def test_phaxio_dry_run(fake_pdf):
    res = send_fax.send_phaxio(fake_pdf, "+14155551234", api_key="DRY",
                               api_secret="DRY", dry_run=True)
    assert res["ok"] and res["dry_run"] and res["provider"] == "phaxio"
    assert res["request"]["fields"]["to"] == "+14155551234"


def test_generic_dry_run(fake_pdf):
    res = send_fax.send_generic(
        fake_pdf, "+14155551234", url="https://api.example.com/v1/faxes",
        auth_header="Bearer XYZ", to_field="fax_number",
        file_field="document", dry_run=True)
    assert res["ok"] and res["dry_run"] and res["provider"] == "generic"
    req = res["request"]
    assert req["url"] == "https://api.example.com/v1/faxes"
    assert req["fields"]["fax_number"] == "+14155551234"
    assert "document" in req["files"]


def test_generic_dry_run_omits_all_authentication_values(fake_pdf):
    import json

    header = "Bearer <API_KEY>"
    user = "<ACCOUNT_NAME>"
    password = "<PASSWORD>"
    for options in ({"auth_header": header}, {"basic_user": user, "basic_pass": password}):
        result = send_fax.send_generic(
            fake_pdf, "+14155551234", url="https://api.example.com/v1/faxes",
            dry_run=True, **options,
        )
        output = json.dumps(result)
        for value in (header, header[:12], user, password):
            assert value not in output
        assert "[REDACTED]" in result["request"]["auth"]


@pytest.mark.parametrize("status_code", [200, 202, 400, 403, 500])
def test_provider_responses_are_never_copied_to_results(status_code):
    class Response:
        @property
        def text(self):
            raise AssertionError("Raw provider text must not be read")

        def json(self):
            raise AssertionError("Provider payload must not be read")

    response = Response()
    response.status_code = status_code
    result = send_fax._result(response)
    assert result["status_code"] == status_code
    assert result["ok"] is (200 <= status_code < 300)
    assert result["response"] == {"message": "Provider response details omitted"}
