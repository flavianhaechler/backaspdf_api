# BackasPDF API

Email to PDF conversion service. Forward emails to `do@backaspdf.com`, get a PDF back.

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                        RAILWAY                                  │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│   [API Service]              [Worker Service]                   │
│   ├── /webhooks/resend       ├── Poll job queue                │
│   ├── /webhooks/stripe       ├── Parse email                    │
│   ├── /billing/*             ├── Render PDF                     │
│   └── /health                └── Send reply                     │
│          │                          │                           │
│          └──────────┬───────────────┘                           │
│                     │                                           │
│              [PostgreSQL]                                       │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

## Project Structure

```
app/
├── main.py           # FastAPI endpoints
├── worker.py         # Job processor
├── config.py         # Settings
│
├── db/
│   ├── __init__.py   # Database queries
│   └── schema.sql    # Tables
│
└── services/
    ├── parse.py      # Email → HTML
    ├── pdf.py        # HTML → PDF
    ├── send.py       # Send emails
    ├── receipt.py    # Receipt extraction
    ├── quota.py      # Usage limits
    ├── stripe.py     # Payments
    └── tokens.py     # Portal tokens
```

## Local Development

```bash
# Create virtual environment
python -m venv venv
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Install Playwright browsers
playwright install chromium

# Copy environment file
cp .env.example .env
# Edit .env with your values

# Start PostgreSQL (Docker)
docker run -d --name backaspdf-db \
  -e POSTGRES_DB=backaspdf \
  -e POSTGRES_USER=postgres \
  -e POSTGRES_PASSWORD=postgres \
  -p 5432:5432 postgres:15

# Run API
uvicorn app.main:app --reload

# Run Worker (separate terminal)
python -m app.worker
```

## Railway Deployment

### Services

1. **API Service**
   - Build: Dockerfile
   - Start: `uvicorn app.main:app --host 0.0.0.0 --port $PORT`
   - Domain: `api.backaspdf.com`

2. **Worker Service**
   - Build: Dockerfile
   - Start: `python -m app.worker`
   - No public domain

3. **PostgreSQL**
   - Railway managed database
   - `DATABASE_URL` auto-configured

### Environment Variables

Set in Railway dashboard:

```
DATABASE_URL          # Auto from Railway Postgres
RESEND_API_KEY        # From Resend dashboard
RESEND_WEBHOOK_SECRET # From Resend dashboard
STRIPE_SECRET_KEY     # From Stripe dashboard
STRIPE_WEBHOOK_SECRET # From Stripe dashboard
STRIPE_PRICE_ID_PRO   # Create in Stripe
TOKEN_SIGNING_SECRET  # Generate: python -c "import secrets; print(secrets.token_hex(32))"
PUBLIC_APP_URL        # https://backaspdf.com
API_BASE_URL          # https://api.backaspdf.com
```

## API Endpoints

### Webhooks

```
POST /webhooks/resend    # Inbound email from Resend
POST /webhooks/stripe    # Stripe events
```

### Billing

```
POST /billing/checkout     # Create Stripe checkout
POST /billing/portal_link  # Generate portal link
GET  /billing/portal       # Redirect to Stripe portal
```

### Health

```
GET /health               # Health check
```

## Consumer Flow

1. User forwards email to `do@backaspdf.com`
2. Resend webhook → API creates job
3. Worker picks up job
4. Parse email → Render PDF → Extract receipt data
5. Send PDF reply from `done@backaspdf.com`
6. Record document in archive

## Developer API (Coming Soon)

```
POST /v1/convert/html   # HTML → PDF
POST /v1/convert/email  # Email → PDF
POST /v1/convert/url    # URL → PDF
GET  /v1/documents/:id  # Get document
GET  /v1/usage          # Usage stats
```

## License

Proprietary - All rights reserved.
