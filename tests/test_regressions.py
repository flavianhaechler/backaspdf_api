import asyncio
import base64
import hashlib
import hmac
import json
import logging
import time
from datetime import datetime
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient
from pypdf import PdfReader, PdfWriter

from app import main, worker
from app.services.parse import normalize_html, parse_email, text_to_html
from app.services.pdf_tools import merge_pdfs, split_pdf
from app.services import quota


FIXTURES = Path(__file__).parent / "fixtures"
WEBHOOK_SECRET = f"whsec_{base64.b64encode(b'test webhook secret').decode()}"


def fixture_html(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def make_pdf(page_count: int) -> bytes:
    writer = PdfWriter()
    for _ in range(page_count):
        writer.add_blank_page(width=612, height=792)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


def signed_headers(body: bytes, secret: str = WEBHOOK_SECRET) -> dict[str, str]:
    message_id = "msg_test_123"
    timestamp = str(int(time.time()))
    secret_bytes = base64.b64decode(secret.removeprefix("whsec_"))
    signed_content = f"{message_id}.{timestamp}.".encode() + body
    digest = hmac.new(secret_bytes, signed_content, hashlib.sha256).digest()
    signature = base64.b64encode(digest).decode()
    return {
        "svix-id": message_id,
        "svix-timestamp": timestamp,
        "svix-signature": f"v1,{signature}",
        "content-type": "application/json",
    }


def test_parse_email_preserves_mail_fields_and_fixture_content():
    cases = [
        ("text_email.html", "Appointment confirmed", "Tuesday at 10:30"),
        ("shop_confirmation.html", "Order NW-2048", "CHF 72.00"),
        ("external_image_email.html", "Monthly update", "images.example.test/monthly-banner.png"),
    ]

    for filename, subject, expected_content in cases:
        parsed = parse_email({
            "messageId": f"message-{filename}",
            "from": "Alex Example <alex@example.test>",
            "subject": subject,
            "html": fixture_html(filename),
            "text": "fallback text",
            "attachments": [],
        })

        assert parsed.message_id == f"message-{filename}"
        assert parsed.from_email == "alex@example.test"
        assert parsed.from_name == "Alex Example"
        assert parsed.subject == subject
        assert expected_content in parsed.html
        assert parsed.text == "fallback text"
        assert parsed.has_attachments is False


def test_normalize_html_wraps_fragment_and_adds_mail_header():
    result = normalize_html("<p>Order is ready.</p>", "Order ready", "shop@example.test", "Shop")

    assert "<!DOCTYPE html>" in result
    assert "<meta charset=\"UTF-8\">" in result
    assert "Order ready" in result
    assert "From: Shop" in result
    assert "<p>Order is ready.</p>" in result


def test_text_to_html_escapes_markup_and_converts_newlines():
    result = text_to_html("A & B\n<receipt>")

    assert "A &amp; B<br>\n&lt;receipt&gt;" in result
    assert "<body style=\"font-family: monospace; white-space: pre-wrap; padding: 20px;\">" in result


def test_generate_filename_uses_date_and_existing_format(monkeypatch):
    class FixedDate(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 11, 19)

    monkeypatch.setattr(worker, "datetime", FixedDate)

    assert worker.generate_filename(subject="Order #2048!") == "2026-11-19_Order-2048.pdf"
    assert worker.generate_filename() == "2026-11-19_document.pdf"
    assert worker.generate_filename(merchant="Northwind Shop", amount=72, currency="CHF") == (
        "2026-11-19_Northwind-Shop_72CHF.pdf"
    )


def test_merge_pdfs_combines_all_pages():
    merged = merge_pdfs([make_pdf(1), make_pdf(2)])

    assert len(PdfReader(BytesIO(merged)).pages) == 3


def test_split_pdf_returns_one_pdf_per_page():
    pages = split_pdf(make_pdf(3))

    assert [filename for filename, _ in pages] == ["page_1.pdf", "page_2.pdf", "page_3.pdf"]
    assert [len(PdfReader(BytesIO(page)).pages) for _, page in pages] == [1, 1, 1]


def configure_quota(monkeypatch, plan, used, warned_upgrade=False, warned_fair_use=False):
    monkeypatch.setattr(quota, "settings", SimpleNamespace(
        free_monthly_limit=10,
        pro_soft_limit=500,
        pro_hard_limit=2000,
    ))
    monkeypatch.setattr(quota.db, "get_sender", AsyncMock(return_value={"plan": plan}))
    monkeypatch.setattr(quota.db, "get_usage", AsyncMock(return_value={
        "count": used,
        "warned_upgrade": warned_upgrade,
        "warned_fair_use": warned_fair_use,
    }))
    monkeypatch.setattr(quota.db, "set_warned_flag", AsyncMock())


def test_free_quota_allows_until_limit_then_warns_once_and_rejects(monkeypatch):
    configure_quota(monkeypatch, "free", 9)
    assert asyncio.run(quota.check_quota("sender@example.test")).action is quota.QuotaAction.ALLOW

    configure_quota(monkeypatch, "free", 10)
    result = asyncio.run(quota.check_quota("sender@example.test"))
    assert result.action is quota.QuotaAction.ALLOW_WARN_UPGRADE
    quota.db.set_warned_flag.assert_awaited_once_with("sender@example.test", "warned_upgrade", True)

    configure_quota(monkeypatch, "free", 10, warned_upgrade=True)
    assert asyncio.run(quota.check_quota("sender@example.test")).action is quota.QuotaAction.REJECT


def test_pro_quota_warns_at_soft_limit_and_rejects_at_hard_limit(monkeypatch):
    configure_quota(monkeypatch, "pro", 499)
    assert asyncio.run(quota.check_quota("sender@example.test")).action is quota.QuotaAction.ALLOW

    configure_quota(monkeypatch, "pro", 500)
    result = asyncio.run(quota.check_quota("sender@example.test"))
    assert result.action is quota.QuotaAction.ALLOW_WARN_FAIR_USE
    quota.db.set_warned_flag.assert_awaited_once_with("sender@example.test", "warned_fair_use", True)

    configure_quota(monkeypatch, "pro", 501, warned_fair_use=True)
    assert asyncio.run(quota.check_quota("sender@example.test")).action is quota.QuotaAction.ALLOW

    configure_quota(monkeypatch, "pro", 2000, warned_fair_use=True)
    assert asyncio.run(quota.check_quota("sender@example.test")).action is quota.QuotaAction.REJECT


def test_resend_webhook_fetches_api_content_and_queues_job(monkeypatch):
    db = main.db
    monkeypatch.setattr(main.settings, "resend_webhook_secret", WEBHOOK_SECRET)
    monkeypatch.setattr(main.settings, "environment", "production")
    monkeypatch.setattr(db, "job_exists", AsyncMock(return_value=False))
    monkeypatch.setattr(db, "create_job", AsyncMock(return_value="job-123"))
    monkeypatch.setattr(main, "check_quota", AsyncMock(return_value=SimpleNamespace(
        action=SimpleNamespace(value="allow"),
        message="",
    )))

    api_calls = []

    class FakeResponse:
        status_code = 200
        text = "not logged by this test"

        @staticmethod
        def json():
            return {"html": "<p>API content</p>", "text": "API text", "headers": []}

    class FakeAsyncClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc_value, traceback):
            return False

        async def get(self, url, headers):
            api_calls.append((url, headers))
            return FakeResponse()

    monkeypatch.setattr(main.httpx, "AsyncClient", FakeAsyncClient)

    payload = {
        "data": {
            "from": "Alex Example <alex@example.test>",
            "to": ["do@backaspdf.com"],
            "messageId": "resend-message-123",
            "email_id": "email-123",
            "subject": "Original subject",
            "html": "<p>Webhook content must not win</p>",
            "text": "Webhook text must not win",
        }
    }
    body = json.dumps(payload, separators=(",", ":")).encode()
    response = TestClient(main.app).post(
        "/webhooks/resend",
        content=body,
        headers=signed_headers(body),
    )

    assert response.status_code == 200
    assert response.json() == {"status": "queued", "job_id": "job-123"}
    assert api_calls[0][0] == "https://api.resend.com/emails/receiving/email-123"
    db.create_job.assert_awaited_once()
    job = db.create_job.await_args.kwargs
    assert job["message_id"] == "resend-message-123"
    assert job["from_email"] == "alex@example.test"
    assert job["subject"] == "Original subject"
    assert job["payload"]["html"] == "<p>API content</p>"
    assert job["payload"]["text"] == "API text"
    assert "Webhook content must not win" not in json.dumps(job["payload"])


def test_resend_webhook_rejects_missing_or_invalid_signature_without_creating_job(monkeypatch):
    monkeypatch.setattr(main.settings, "resend_webhook_secret", WEBHOOK_SECRET)
    monkeypatch.setattr(main.settings, "environment", "production")
    create_job = AsyncMock()
    monkeypatch.setattr(main.db, "create_job", create_job)
    body = b'{"data":{"subject":"must not be logged"}}'

    client = TestClient(main.app)
    missing = client.post("/webhooks/resend", content=body)
    invalid_headers = signed_headers(body)
    invalid_headers["svix-signature"] = "v1,invalid"
    invalid = client.post("/webhooks/resend", content=body, headers=invalid_headers)

    assert missing.status_code == 401
    assert invalid.status_code == 401
    create_job.assert_not_awaited()


def test_invalid_resend_signature_logs_once_without_payload_content(monkeypatch, caplog):
    monkeypatch.setattr(main.settings, "resend_webhook_secret", WEBHOOK_SECRET)
    monkeypatch.setattr(main.settings, "environment", "production")
    body = b'{"data":{"subject":"private subject marker"}}'

    with caplog.at_level(logging.WARNING, logger="app.main"):
        response = TestClient(main.app).post("/webhooks/resend", content=body)

    webhook_records = [record for record in caplog.records if record.name == "app.main"]
    assert response.status_code == 401
    assert len(webhook_records) == 1
    assert "private subject marker" not in webhook_records[0].getMessage()


def test_resend_webhook_fails_closed_in_production_without_secret(monkeypatch):
    monkeypatch.setattr(main.settings, "resend_webhook_secret", "")
    monkeypatch.setattr(main.settings, "environment", "production")
    monkeypatch.setattr(main.settings, "resend_webhook_allow_unsigned_local", False, raising=False)
    create_job = AsyncMock()
    monkeypatch.setattr(main.db, "create_job", create_job)

    response = TestClient(main.app).post(
        "/webhooks/resend",
        content=b'{"data":{}}',
    )

    assert response.status_code == 401
    create_job.assert_not_awaited()


def test_resend_webhook_requires_email_id_even_with_valid_signature(monkeypatch):
    monkeypatch.setattr(main.settings, "resend_webhook_secret", WEBHOOK_SECRET)
    monkeypatch.setattr(main.settings, "environment", "production")
    create_job = AsyncMock()
    monkeypatch.setattr(main.db, "create_job", create_job)
    body = json.dumps({"data": {
        "from": "alex@example.test",
        "messageId": "message-without-email-id",
        "to": ["do@backaspdf.com"],
    }}, separators=(",", ":")).encode()

    response = TestClient(main.app).post(
        "/webhooks/resend",
        content=body,
        headers=signed_headers(body),
    )

    assert response.status_code == 400
    create_job.assert_not_awaited()


def test_resend_webhook_allows_unsigned_request_only_with_local_opt_in(monkeypatch):
    monkeypatch.setattr(main.settings, "resend_webhook_secret", "")
    monkeypatch.setattr(main.settings, "environment", "development")
    monkeypatch.setattr(main.settings, "resend_webhook_allow_unsigned_local", True, raising=False)
    monkeypatch.setattr(main.db, "job_exists", AsyncMock(return_value=False))
    monkeypatch.setattr(main.db, "create_job", AsyncMock(return_value="local-job"))
    monkeypatch.setattr(main, "check_quota", AsyncMock(return_value=SimpleNamespace(
        action=SimpleNamespace(value="allow"),
        message="",
    )))

    class FakeResponse:
        status_code = 200

        @staticmethod
        def json():
            return {"html": "<p>Local API content</p>", "text": "Local API text"}

    class FakeAsyncClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc_value, traceback):
            return False

        async def get(self, url, headers):
            return FakeResponse()

    monkeypatch.setattr(main.httpx, "AsyncClient", FakeAsyncClient)
    response = TestClient(main.app).post("/webhooks/resend", json={
        "data": {
            "from": "alex@example.test",
            "to": ["do@backaspdf.com"],
            "messageId": "local-message",
            "email_id": "local-email",
        }
    })

    assert response.status_code == 200
    assert response.json() == {"status": "queued", "job_id": "local-job"}


def test_resend_api_failure_does_not_create_job(monkeypatch):
    monkeypatch.setattr(main.settings, "resend_webhook_secret", WEBHOOK_SECRET)
    monkeypatch.setattr(main.settings, "environment", "production")
    create_job = AsyncMock()
    monkeypatch.setattr(main.db, "create_job", create_job)

    class FakeResponse:
        status_code = 503

    class FakeAsyncClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc_value, traceback):
            return False

        async def get(self, url, headers):
            return FakeResponse()

    monkeypatch.setattr(main.httpx, "AsyncClient", FakeAsyncClient)
    payload = {"data": {
        "from": "alex@example.test",
        "to": ["do@backaspdf.com"],
        "messageId": "failed-fetch-message",
        "email_id": "failed-fetch-email",
    }}
    body = json.dumps(payload, separators=(",", ":")).encode()
    response = TestClient(main.app).post(
        "/webhooks/resend",
        content=body,
        headers=signed_headers(body),
    )

    assert response.status_code == 502
    create_job.assert_not_awaited()
