## Back as PDF API

Forward an email to `do@backaspdf.com` and receive a PDF from `done@backaspdf.com`.
Messages sent to `merge@backaspdf.com` or `split@backaspdf.com` process PDF attachments.

## Architecture

The API validates Resend webhooks, fetches email content using Resend's API, checks
the sender's monthly fair-use limit, and stores a job in PostgreSQL. A separate
worker parses the job, renders or transforms PDF files, sends the result, and
records document metadata.

```text
Resend -> API -> PostgreSQL job queue -> Worker -> Resend reply
                     |
                 Documents
```

## Fair use

The service is free and uses one monthly conversion limit per sender. Configure it
with `MONTHLY_CONVERSION_LIMIT`; the default is `10`. A sender receives one notice
when reaching the limit. Further requests are declined until the next month.

## Billing

Billing is disabled by default. `BILLING_ENABLED=false` makes
`/webhooks/stripe` and all `/billing/*` routes return 404. Stripe code remains
available but Stripe environment variables are not needed while billing is off.

## Privacy / Datenschutz

- Logs omit message bodies, subjects, sender addresses, and raw API responses.
- After a job reaches `completed`, `failed`, or `rejected`, its `email_payload`
  is replaced with an empty JSON object. The `message_id` remains stored for
  webhook deduplication.
- To count existing terminal jobs with retained payloads, run
  `python -m scripts.cleanup_terminal_email_payloads --dry-run`. This is a
  read-only count. The script changes data only when explicitly invoked with
  `--execute`; it is not run automatically.

## Webhook and environment configuration

Resend webhook requests are verified using `RESEND_WEBHOOK_SECRET` and the
original request body. In local development only, unsigned requests may be
allowed when `RESEND_WEBHOOK_SECRET` is empty and
`RESEND_WEBHOOK_ALLOW_UNSIGNED_LOCAL=true` is set together with
`ENVIRONMENT=development`. If a secret is configured, its signature is always
required. Keep the local override false in production.

The API and Worker both need `DATABASE_URL`, `RESEND_API_KEY`,
`MONTHLY_CONVERSION_LIMIT`, and the sender/application settings in `.env.example`.
The API additionally needs `RESEND_WEBHOOK_SECRET`; the Worker uses
`WORKER_POLL_INTERVAL` and `WORKER_BATCH_SIZE`. Leave `BILLING_ENABLED=false`;
no Stripe variables are required.

## Local development

```bash
python -m venv .venv
python -m pip install -r requirements.txt
playwright install chromium
```

Copy `.env.example` to `.env` and set local values. Run PostgreSQL, then start:

```bash
uvicorn app.main:app --reload
python -m app.worker
```

Run the automated tests with `python -m pytest tests -q`. They use mocks and do
not require a real database or Resend API. The optional manual conversion example
is `python tests/manual_conversion.py`.

## Railway deployment

- **API:** `uvicorn app.main:app --host 0.0.0.0 --port $PORT`
- **Worker:** `python -m app.worker`
- **PostgreSQL:** configure Railway's `DATABASE_URL` for both services.

## Endpoints

- `POST /webhooks/resend` — verified inbound Resend webhook.
- `GET /health` — health check.
- `POST /webhooks/stripe` and `/billing/*` — return 404 while billing is disabled.

## License

MIT License — see [LICENSE](./LICENSE) for details.
