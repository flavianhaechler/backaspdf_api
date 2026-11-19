import asyncio
import base64
from contextlib import asynccontextmanager
import hashlib
import hmac
import json
import logging
import time
from datetime import datetime
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from fastapi.testclient import TestClient
from pypdf import PdfReader, PdfWriter
import pytest

from app import db, main, worker
from app.services import pdf as pdf_service
from app.services.parse import extract_pdf_attachments, normalize_html, parse_email, text_to_html
from app.services.pdf import close_browser, render_pdf
from app.services.pdf_tools import merge_pdfs, split_pdf
from app.services import quota
from app.config import Settings
from app.services import send


FIXTURES = Path(__file__).parent / "fixtures"
WEBHOOK_SECRET = f"whsec_{base64.b64encode(b'test webhook secret').decode()}"
PDF_BASELINES = {
    "text_email.html": {
        "pages": 1,
        "text": (
            "Appointment confirmed\nFrom: Alex Example\nHello,\n"
            "Your appointment is confirmed for Tuesday at 10:30.\nThank you,\nAlex"
        ),
    },
    "shop_confirmation.html": {
        "pages": 1,
        "text": (
            "Order NW-2048\nFrom: Northwind Shop\n"
            "Northwind Shop - Order confirmation\nOrder NW-2048 has been confirmed.\n"
            "Item Quantity Price\nDesk lamp1 CHF 48.00\nNotebook2 CHF 12.00\nTotal: CHF 72.00"
        ),
    },
    "external_image_email.html": {
        "pages": 1,
        "text": (
            "Monthly update\nFrom: Example Studio\nMonthly update\n"
            "Here is the latest update from Example Studio.\nExample Studio banner"
        ),
    },
}


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


def test_parse_email_does_not_log_mail_content_or_subject(caplog):
    markers = ("private subject marker", "private html marker", "private text marker")
    payload = {
        "messageId": "private-message-id",
        "from": "private-sender@example.test",
        "subject": markers[0],
        "html": f"<p>{markers[1]}</p>",
        "text": markers[2],
        "attachments": [],
    }

    with caplog.at_level(logging.INFO, logger="app.services.parse"):
        parse_email(payload)

    assert all(marker not in caplog.text for marker in markers)


def test_attachment_download_logs_do_not_include_raw_response_or_filename(monkeypatch, caplog):
    response_marker = "private-attachment-response"
    filename_marker = "private-attachment-name.pdf"

    class FakeResponse:
        status_code = 200
        text = ""
        content = b"pdf-bytes"

        def json(self):
            return {"data": [{
                "filename": filename_marker,
                "content_type": "application/pdf",
                "download_url": "https://files.example.test/private.pdf",
                "metadata": response_marker,
            }]}

    class FakeAsyncClient:
        def __init__(self, timeout=None):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc_value, traceback):
            return False

        async def get(self, url, headers=None):
            return FakeResponse()

    monkeypatch.setattr("app.services.parse.httpx.AsyncClient", FakeAsyncClient)

    with caplog.at_level(logging.INFO, logger="app.services.parse"):
        pdfs = asyncio.run(extract_pdf_attachments("private-email-id"))

    assert pdfs == [b"pdf-bytes"]
    assert response_marker not in caplog.text
    assert filename_marker not in caplog.text
    assert "private-email-id" not in caplog.text


def test_attachment_api_error_does_not_log_raw_response(monkeypatch, caplog):
    response_marker = "private-api-response-body"

    class FakeResponse:
        status_code = 503
        text = response_marker

    class FakeAsyncClient:
        def __init__(self, timeout=None):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc_value, traceback):
            return False

        async def get(self, url, headers=None):
            return FakeResponse()

    monkeypatch.setattr("app.services.parse.httpx.AsyncClient", FakeAsyncClient)

    with caplog.at_level(logging.ERROR, logger="app.services.parse"):
        assert asyncio.run(extract_pdf_attachments("private-email-id")) == []

    assert response_marker not in caplog.text


def test_fixture_pdfs_match_pre_hardening_pages_and_extracted_text():
    cases = {
        "text_email.html": ("Appointment confirmed", "Alex Example"),
        "shop_confirmation.html": ("Order NW-2048", "Northwind Shop"),
        "external_image_email.html": ("Monthly update", "Example Studio"),
    }

    async def render_fixtures():
        results = {}
        try:
            for filename, (subject, from_name) in cases.items():
                parsed = parse_email({
                    "messageId": filename,
                    "from": f"{from_name} <sender@example.test>",
                    "subject": subject,
                    "html": fixture_html(filename),
                    "text": "",
                    "attachments": [],
                })
                pdf = await render_pdf(parsed.html)
                reader = PdfReader(BytesIO(pdf))
                results[filename] = {
                    "pages": len(reader.pages),
                    "text": "\n".join(page.extract_text() or "" for page in reader.pages),
                }
            return results
        finally:
            await close_browser()

    assert asyncio.run(render_fixtures()) == PDF_BASELINES


def test_normalize_html_wraps_fragment_and_adds_mail_header():
    result = normalize_html("<p>Order is ready.</p>", "Order ready", "shop@example.test", "Shop")

    assert "<!DOCTYPE html>" in result
    assert "<meta charset=\"UTF-8\">" in result
    assert "Order ready" in result
    assert "From: Shop" in result
    assert "<p>Order is ready.</p>" in result


def test_normalize_html_escapes_subject_and_sender_in_header():
    subject = '<script>alert("mail")</script>'
    from_name = '"><img src=x onerror="mail">'

    result = normalize_html("<p>Body</p>", subject, "sender@example.test", from_name)

    assert "&lt;script&gt;alert(&quot;mail&quot;)&lt;/script&gt;" in result
    assert "&quot;&gt;&lt;img src=x onerror=&quot;mail&quot;&gt;" in result
    assert subject not in result
    assert from_name not in result


@pytest.mark.parametrize(
    ("url", "resource_type", "expected"),
    [
        ("data:image/png;base64,AA==", "image", True),
        ("data:text/css,body%7Bcolor%3Ared%7D", "stylesheet", True),
        ("http://93.184.216.34/image.png", "image", True),
        ("https://93.184.216.34/style.css", "stylesheet", True),
        ("https://93.184.216.34/font.woff2", "font", True),
        ("https://93.184.216.34/app.js", "script", False),
        ("https://93.184.216.34/page.html", "document", False),
        ("ftp://93.184.216.34/image.png", "image", False),
        ("file:///etc/passwd", "image", False),
        ("http://127.0.0.1/image.png", "image", False),
        ("http://10.1.2.3/image.png", "image", False),
        ("http://172.16.0.1/image.png", "image", False),
        ("http://192.168.1.1/image.png", "image", False),
        ("http://169.254.169.254/latest/meta-data", "image", False),
        ("http://100.64.0.1/image.png", "image", False),
        ("http://[::1]/image.png", "image", False),
        ("http://[fc00::1]/image.png", "image", False),
        ("http://[fe80::1]/image.png", "image", False),
        ("http://[::ffff:10.0.0.1]/image.png", "image", False),
    ],
)
def test_render_request_allowlist_for_schemes_types_and_ip_literals(url, resource_type, expected):
    assert asyncio.run(pdf_service._is_allowed_render_request(url, resource_type)) is expected


def test_render_request_allowlist_rejects_dns_names_resolving_to_private_addresses(monkeypatch):
    async def resolve_to_private(host, port):
        return [(None, None, None, None, ("10.0.0.8", port))]

    monkeypatch.setattr(pdf_service, "_resolve_host_addresses", resolve_to_private)

    assert not asyncio.run(pdf_service._is_allowed_render_request(
        "https://assets.example.test/image.png",
        "image",
    ))


def test_render_request_allowlist_accepts_public_dns_names(monkeypatch):
    async def resolve_to_public(host, port):
        return [(None, None, None, None, ("93.184.216.34", port))]

    monkeypatch.setattr(pdf_service, "_resolve_host_addresses", resolve_to_public)

    assert asyncio.run(pdf_service._is_allowed_render_request(
        "https://assets.example.test/image.png",
        "image",
    ))


@pytest.mark.parametrize("allowed", [True, False])
def test_render_request_route_continues_or_aborts(monkeypatch, allowed):
    checker = AsyncMock(return_value=allowed)
    monkeypatch.setattr(pdf_service, "_is_allowed_render_request", checker)

    class FakeRequest:
        url = "https://assets.example.test/image.png"
        resource_type = "image"

    class FakeRoute:
        request = FakeRequest()

        def __init__(self):
            self.continue_ = AsyncMock()
            self.abort = AsyncMock()

    route = FakeRoute()
    asyncio.run(pdf_service._route_render_request(route))

    checker.assert_awaited_once_with(route.request.url, route.request.resource_type)
    assert route.continue_.await_count == int(allowed)
    assert route.abort.await_count == int(not allowed)


def test_render_pdf_disables_javascript_and_installs_request_route(monkeypatch):
    class FakePage:
        async def set_content(self, html, wait_until):
            return None

        async def goto(self, url, wait_until):
            return None

        async def pdf(self, **options):
            return b"pdf"

    class FakeContext:
        def __init__(self):
            self.options = []
            self.route_pattern = None
            self.route_handler = None

        def set_default_timeout(self, timeout):
            self.timeout = timeout

        async def route(self, pattern, handler):
            self.route_pattern = pattern
            self.route_handler = handler

        async def new_page(self):
            return FakePage()

        async def close(self):
            return None

    class FakeBrowser:
        def __init__(self, context):
            self.context = context

        async def new_context(self, **options):
            self.context.options.append(options)
            return self.context

    context = FakeContext()
    monkeypatch.setattr(pdf_service, "get_browser", AsyncMock(return_value=FakeBrowser(context)))

    async def render_both():
        email_pdf = await pdf_service.render_pdf("<p>safe content</p>")
        url_pdf = await pdf_service.render_url_to_pdf("https://example.test/")
        return email_pdf, url_pdf

    assert asyncio.run(render_both()) == (b"pdf", b"pdf")
    assert [options["java_script_enabled"] for options in context.options] == [False, False]
    assert context.route_pattern == "**/*"
    assert context.route_handler is not None


def test_text_to_html_escapes_markup_and_converts_newlines():
    result = text_to_html("A & B\n<receipt>")

    assert "A &amp; B<br>\n&lt;receipt&gt;" in result
    assert "<body style=\"font-family: monospace; white-space: pre-wrap; padding: 20px;\">" in result


def test_email_send_error_does_not_log_recipient_body_or_response(monkeypatch, caplog):
    markers = ("private-recipient@example.test", "private subject", "private email body", "private API response")

    class FakeResponse:
        status_code = 503
        text = markers[3]

    class FakeAsyncClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc_value, traceback):
            return False

        async def post(self, url, headers, json, timeout):
            return FakeResponse()

    monkeypatch.setattr(send.httpx, "AsyncClient", FakeAsyncClient)

    with caplog.at_level(logging.ERROR, logger="app.services.send"):
        result = asyncio.run(send._send_email({
            "to": [markers[0]],
            "subject": markers[1],
            "html": markers[2],
        }))

    assert result is False
    assert all(marker not in caplog.text for marker in markers)


def test_successful_email_send_does_not_log_recipient_or_api_response(monkeypatch, caplog):
    markers = ("private-recipient@example.test", "private-api-response-id")

    class FakeResponse:
        status_code = 200
        text = ""

        @staticmethod
        def json():
            return {"id": markers[1]}

    class FakeAsyncClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc_value, traceback):
            return False

        async def post(self, url, headers, json, timeout):
            return FakeResponse()

    monkeypatch.setattr(send.httpx, "AsyncClient", FakeAsyncClient)

    with caplog.at_level(logging.INFO, logger="app.services.send"):
        result = asyncio.run(send._send_email({
            "to": [markers[0]],
            "subject": "private subject",
            "html": "private body",
        }))

    assert result is True
    assert all(marker not in caplog.text for marker in markers)


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


@pytest.mark.parametrize("status", ["completed", "failed", "rejected", "processing"])
def test_update_job_status_clears_payload_only_for_terminal_statuses(monkeypatch, status):
    connection = SimpleNamespace(execute=AsyncMock())

    @asynccontextmanager
    async def fake_get_conn():
        yield connection

    monkeypatch.setattr(db, "get_conn", fake_get_conn)
    asyncio.run(db.update_job_status("job-123", status, "safe-error"))

    query, job_id, saved_status, error = connection.execute.await_args.args
    normalized_query = " ".join(query.split())
    assert "email_payload = CASE" in query
    assert "THEN '{}'::jsonb ELSE email_payload END" in normalized_query
    assert "('completed', 'failed', 'rejected')" in query
    assert (job_id, saved_status, error) == ("job-123", status, "safe-error")


def test_job_deduplication_still_uses_message_id(monkeypatch):
    connection = SimpleNamespace(fetchrow=AsyncMock(return_value={"exists": 1}))

    @asynccontextmanager
    async def fake_get_conn():
        yield connection

    monkeypatch.setattr(db, "get_conn", fake_get_conn)

    assert asyncio.run(db.job_exists("stable-message-id"))
    query, message_id = connection.fetchrow.await_args.args
    assert "WHERE message_id = $1" in query
    assert message_id == "stable-message-id"


def test_worker_logs_do_not_include_sender_subject_or_content(monkeypatch, caplog):
    markers = ("private-sender@example.test", "private subject", "private body")
    monkeypatch.setattr(worker.db, "update_job_status", AsyncMock())
    monkeypatch.setattr(worker, "_process_render", AsyncMock())

    with caplog.at_level(logging.INFO, logger="app.worker"):
        result = asyncio.run(worker.process_job({
            "id": "job-privacy-123",
            "from_email": markers[0],
            "job_type": "render",
            "email_payload": {"subject": markers[1], "html": markers[2]},
        }))

    assert result is True
    assert "job-privacy-123" in caplog.text
    assert all(marker not in caplog.text for marker in markers)


def test_worker_failure_logs_and_stores_only_exception_type(monkeypatch, caplog):
    private_error = "private body leaked by renderer"
    update_status = AsyncMock()
    monkeypatch.setattr(worker.db, "update_job_status", update_status)
    monkeypatch.setattr(
        worker,
        "_process_render",
        AsyncMock(side_effect=RuntimeError(private_error)),
    )

    with caplog.at_level(logging.ERROR, logger="app.worker"):
        result = asyncio.run(worker.process_job({
            "id": "job-failed-123",
            "from_email": "sender@example.test",
            "job_type": "render",
            "email_payload": {},
        }))

    assert result is False
    assert private_error not in caplog.text
    assert update_status.await_args_list[-1].args == ("job-failed-123", "failed", "RuntimeError")


def test_terminal_payload_cleanup_dry_run_counts_without_update(monkeypatch):
    from scripts import cleanup_terminal_email_payloads

    connection = SimpleNamespace(
        fetchval=AsyncMock(return_value=3),
        execute=AsyncMock(),
        close=AsyncMock(),
    )
    monkeypatch.setattr(
        cleanup_terminal_email_payloads.asyncpg,
        "connect",
        AsyncMock(return_value=connection),
    )

    result = asyncio.run(cleanup_terminal_email_payloads.run_cleanup(execute=False))

    assert result == 3
    connection.fetchval.assert_awaited_once()
    connection.execute.assert_not_awaited()
    connection.close.assert_awaited_once()


def configure_quota(monkeypatch, plan, used, warned_upgrade=False, warned_fair_use=False):
    monkeypatch.setattr(quota, "settings", SimpleNamespace(
        monthly_conversion_limit=10,
    ))
    monkeypatch.setattr(quota.db, "get_sender", AsyncMock(return_value={"plan": plan}))
    monkeypatch.setattr(quota.db, "get_usage", AsyncMock(return_value={
        "count": used,
        "warned_upgrade": warned_upgrade,
        "warned_fair_use": warned_fair_use,
    }))
    monkeypatch.setattr(quota.db, "set_warned_flag", AsyncMock())


def test_monthly_quota_allows_until_limit_then_warns_once_and_rejects(monkeypatch):
    configure_quota(monkeypatch, "free", 9)
    assert asyncio.run(quota.check_quota("sender@example.test")).action is quota.QuotaAction.ALLOW

    configure_quota(monkeypatch, "free", 10)
    result = asyncio.run(quota.check_quota("sender@example.test"))
    assert result.action is quota.QuotaAction.ALLOW_WARN_UPGRADE
    quota.db.set_warned_flag.assert_awaited_once_with("sender@example.test", "warned_upgrade", True)

    configure_quota(monkeypatch, "free", 10, warned_upgrade=True)
    assert asyncio.run(quota.check_quota("sender@example.test")).action is quota.QuotaAction.REJECT


def test_monthly_quota_is_the_same_for_every_sender_plan(monkeypatch):
    configure_quota(monkeypatch, "pro", 9)
    assert asyncio.run(quota.check_quota("sender@example.test")).action is quota.QuotaAction.ALLOW

    configure_quota(monkeypatch, "pro", 10)
    result = asyncio.run(quota.check_quota("sender@example.test"))
    assert result.action is quota.QuotaAction.ALLOW_WARN_UPGRADE
    quota.db.set_warned_flag.assert_awaited_once_with("sender@example.test", "warned_upgrade", True)

    configure_quota(monkeypatch, "pro", 10, warned_upgrade=True)
    assert asyncio.run(quota.check_quota("sender@example.test")).action is quota.QuotaAction.REJECT
    configure_quota(monkeypatch, "pro", 11)
    result = asyncio.run(quota.check_quota("sender@example.test"))
    assert result.action is quota.QuotaAction.REJECT
    assert "Upgrade" not in result.message
    assert "Pro" not in result.message


def test_limit_notice_and_rejection_emails_have_fair_use_only_copy(monkeypatch):
    send_email = AsyncMock(return_value=True)
    monkeypatch.setattr(send, "_send_email", send_email)

    asyncio.run(send.send_upgrade_email("sender@example.test", 10, 10))
    notice = send_email.await_args.args[0]
    assert notice["subject"] == "Monthly conversion limit reached — Back as PDF"
    assert "You've used <strong>10 of 10</strong> conversions this month." in notice["html"]
    assert "Further requests will be declined" in notice["html"]
    assert "Upgrade" not in notice["html"]
    assert "Pro" not in notice["html"]
    assert "/subscribe" not in notice["html"]

    send_email.reset_mock()
    asyncio.run(send.send_fair_use_email("sender@example.test", 10, 10))
    fair_use_notice = send_email.await_args.args[0]
    assert "Pro" not in fair_use_notice["html"]
    assert "Upgrade" not in fair_use_notice["html"]

    send_email.reset_mock()
    asyncio.run(send.send_rejection_email(
        "sender@example.test",
        "Monthly limit of 10 conversions reached. Your limit resets at the start of next month.",
    ))
    rejection = send_email.await_args.args[0]
    assert "Monthly limit of 10 conversions reached." in rejection["html"]
    assert "Upgrade" not in rejection["html"]
    assert "Pro" not in rejection["html"]
    assert "/subscribe" not in rejection["html"]


def test_resend_webhook_fetches_api_content_and_queues_job(monkeypatch, caplog):
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
    with caplog.at_level(logging.INFO, logger="app.main"):
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
    assert "alex@example.test" not in caplog.text
    assert "Original subject" not in caplog.text
    assert "Webhook content must not win" not in caplog.text
    assert "API content" not in caplog.text


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


def test_billing_is_disabled_by_default():
    assert Settings(_env_file=None).billing_enabled is False


def test_monthly_conversion_limit_defaults_to_the_previous_free_limit():
    assert Settings(_env_file=None).monthly_conversion_limit == 10


def test_monthly_conversion_limit_reads_environment_variable(monkeypatch):
    monkeypatch.setenv("MONTHLY_CONVERSION_LIMIT", "15")

    assert Settings(_env_file=None).monthly_conversion_limit == 15


def test_billing_routes_return_404_when_disabled_without_side_effects(monkeypatch):
    monkeypatch.setattr(main.settings, "billing_enabled", False, raising=False)
    stripe_event = MagicMock(return_value={"type": "unknown", "data": {"object": {}}})
    checkout = AsyncMock(return_value="https://stripe.example.test/checkout")
    portal_session = AsyncMock(return_value="https://stripe.example.test/portal")
    get_sender = AsyncMock(return_value={"stripe_customer_id": "cus_test"})
    verify_token = MagicMock(return_value={"email": "alex@example.com"})
    monkeypatch.setattr(main.stripe, "construct_webhook_event", stripe_event)
    monkeypatch.setattr(main.stripe, "create_checkout_session", checkout)
    monkeypatch.setattr(main.stripe, "create_portal_session", portal_session)
    monkeypatch.setattr(main.db, "get_sender", get_sender)
    monkeypatch.setattr(main.tokens, "verify_portal_token", verify_token)

    client = TestClient(main.app)
    responses = [
        client.post("/webhooks/stripe", content=b"{}"),
        client.post("/billing/checkout", json={"email": "alex@example.com"}),
        client.post("/billing/portal_link", json={"email": "alex@example.com"}),
        client.get("/billing/portal?token=test-token"),
    ]

    assert [response.status_code for response in responses] == [404, 404, 404, 404]
    stripe_event.assert_not_called()
    checkout.assert_not_awaited()
    portal_session.assert_not_awaited()
    get_sender.assert_not_awaited()
    verify_token.assert_not_called()


def test_billing_routes_keep_existing_behavior_when_enabled(monkeypatch):
    monkeypatch.setattr(main.settings, "billing_enabled", True, raising=False)
    stripe_event = MagicMock(return_value={"type": "unknown", "data": {"object": {}}})
    checkout = AsyncMock(return_value="https://stripe.example.test/checkout")
    portal_session = AsyncMock(return_value="https://stripe.example.test/portal")
    get_sender = AsyncMock(return_value={"stripe_customer_id": "cus_test"})
    create_token = MagicMock(return_value="signed-token")
    verify_token = MagicMock(return_value={"email": "alex@example.com"})
    monkeypatch.setattr(main.stripe, "construct_webhook_event", stripe_event)
    monkeypatch.setattr(main.stripe, "create_checkout_session", checkout)
    monkeypatch.setattr(main.stripe, "create_portal_session", portal_session)
    monkeypatch.setattr(main.db, "get_sender", get_sender)
    monkeypatch.setattr(main.tokens, "create_portal_token", create_token)
    monkeypatch.setattr(main.tokens, "verify_portal_token", verify_token)

    client = TestClient(main.app, follow_redirects=False)
    webhook = client.post("/webhooks/stripe", content=b"{}")
    checkout_response = client.post("/billing/checkout", json={"email": "alex@example.com"})
    portal_link = client.post("/billing/portal_link", json={"email": "alex@example.com"})
    portal = client.get("/billing/portal?token=signed-token")

    assert webhook.status_code == 200
    assert webhook.json() == {"received": True}
    assert checkout_response.status_code == 200
    assert checkout_response.json() == {"url": "https://stripe.example.test/checkout"}
    assert portal_link.status_code == 200
    assert portal_link.json()["url"].endswith("/billing/portal?token=signed-token")
    assert portal.status_code == 302
    assert portal.headers["location"] == "https://stripe.example.test/portal"
    stripe_event.assert_called_once()
    checkout.assert_awaited_once_with("alex@example.com")
    portal_session.assert_awaited_once_with("cus_test")
