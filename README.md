# Ledgix — Backend

Django 6 API server for Ledgix, an AI-powered invoice processing platform. Handles invoice extraction via Mindee, multi-stage duplicate detection, real-time WebSocket updates, Gmail integration, and Stripe billing.

## Stack

| | |
|---|---|
| **Framework** | Django 6.0 + Django REST Framework 3.17 |
| **ASGI Server** | Daphne 4.1 (required for WebSocket support) |
| **Task Queue** | Celery 5.4 + Celery Beat, Redis broker |
| **Real-time** | Django Channels 4.2 + Channels Redis |
| **AI Extraction** | Mindee V2 API (custom-trained invoice model) |
| **Embeddings / LLM** | OpenAI `text-embedding-3-small` + GPT-4o-mini |
| **Fuzzy Matching** | RapidFuzz 3.0 |
| **Email** | Resend |
| **Gmail** | Gmail API + Google Cloud Pub/Sub |
| **Billing** | Stripe 10.0 |
| **Auth** | SimpleJWT 5.5, Google OAuth2 |
| **Config** | python-decouple |

## Prerequisites

- Python 3.12+
- Redis (Celery broker and Channels layer)
- Mindee API key with a trained invoice model

## Local Setup

```bash
python -m venv venv
source venv/bin/activate

pip install -r requirements.txt

cd src
cp ../.env.example ../.env   # fill in values
python manage.py migrate
python manage.py createsuperuser
```

Run each of the following in a separate terminal:

```bash
# API server (with WebSocket support)
daphne -b 0.0.0.0 -p 8000 config.asgi:application

# Background worker
celery -A config worker -l info

# Scheduled tasks (Gmail sync, watch renewal, etc.)
celery -A config beat -l info --scheduler django_celery_beat.schedulers:DatabaseScheduler
```

If you only need the REST API and don't care about WebSockets, `python manage.py runserver` works fine for development.

## Environment Variables

```bash
# Django
SECRET_KEY=
DEBUG=True
ALLOWED_HOSTS=localhost,127.0.0.1
CORS_ALLOWED_ORIGINS=http://localhost:3000

# URLs (used in email links and OAuth redirects)
BACKEND_URL=http://localhost:8000
FRONTEND_URL=http://localhost:3000

# Redis
REDIS_URL=redis://localhost:6379

# Mindee (invoice extraction)
MINDEE_API_KEY=
MINDEE_V2_API_KEY=
MINDEE_MODEL_ID=

# OpenAI (optional — enables embedding-based duplicate detection and LLM verification)
OPEN_AI_API_KEY=

# Gmail OAuth (separate from the frontend's sign-in credentials)
GOOGLE_CLIENT_ID_EMAIL=
GOOGLE_CLIENT_SECRET_EMAIL=
GMAIL_OAUTH_REDIRECT_URI=http://localhost:8000/api/gmail/callback/

# Gmail Pub/Sub (optional — enables push notifications instead of polling)
GMAIL_PUBSUB_TOPIC=projects/<project>/topics/<topic>

# Email (optional — signup/invite emails will silently no-op if unset)
RESEND_API_KEY=
RESEND_FROM_EMAIL=support@yourapp.com

# Stripe (optional — required for billing features)
DEV_STRIPE_SECRET_KEY=sk_test_...
DEV_STRIPE_PUBLISHABLE_KEY=pk_test_...
DEV_STRIPE_PRO_PRICE_ID=price_...
STRIPE_WEBHOOK_SECRET=
```

## Project Structure

```
src/
├── config/
│   ├── settings.py       # Django settings, Celery Beat schedule, Channels config
│   ├── asgi.py           # ASGI app with WebSocket routing
│   ├── celery.py         # Celery app and autodiscovery
│   └── urls.py           # Root URL conf
│
├── users/                # Accounts and authentication
│   ├── models.py         # User (email as PK), PasswordResetToken, PendingRegistration
│   ├── views.py          # Register, login, Google auth, email verify, password reset
│   └── tasks.py          # Prune unverified registrations after 24h
│
├── organizations/        # Multi-tenant workspaces
│   ├── models.py         # Organization, Membership, Invitation, ActivityLog
│   ├── mixins.py         # OrgScopedMixin — resolves org from header, enforces access
│   ├── permissions.py    # Role-based permission classes
│   └── views.py          # Org CRUD, member management, invitations, activity log
│
├── invoices/             # Core product
│   ├── models.py         # Invoice, DuplicateCheckResult, Notification
│   ├── tasks.py          # process_invoice, check_invoice_duplicates (Celery)
│   ├── consumers.py      # WebSocket consumer — broadcasts to per-user channel group
│   ├── dashboard.py      # Aggregation queries for dashboard metrics and AI insights
│   └── views.py          # Upload, list, detail, approve/reject, bulk actions
│
├── gmail_integration/    # Gmail OAuth and attachment sync
│   ├── models.py         # GmailIntegration (OAuth tokens, watch state), GmailSyncedMessage
│   ├── service.py        # Gmail API client, OAuth helpers, attachment download
│   ├── tasks.py          # sync_gmail_invoices (every 2 min), setup_watch (every 12h)
│   └── views.py          # Auth flow, sync trigger, status, Pub/Sub webhook
│
└── billing/              # Stripe subscriptions
    └── views.py          # Checkout session, portal, plan status, Stripe webhooks
```

## API

All endpoints are under `/api/`. Authentication uses JWT bearer tokens. Multi-tenant routing is via the `X-Organization-Id` request header (falls back to the user's first org if omitted).

**Auth**
```
POST   /api/auth/register/
POST   /api/auth/verify-email/
POST   /api/auth/resend-verification/
POST   /api/auth/login/
POST   /api/auth/google/
POST   /api/auth/token/refresh/
GET    /api/auth/me/
POST   /api/auth/forgot-password/
POST   /api/auth/reset-password/
```

**Invoices**
```
GET    /api/invoices/
POST   /api/invoices/upload/             # multipart/form-data, accepts multiple files
GET    /api/invoices/<id>/
PATCH  /api/invoices/<id>/               # edit extracted fields, rejection reason
DELETE /api/invoices/<id>/
POST   /api/invoices/<id>/process/       # enqueue AI extraction (re-process)
POST   /api/invoices/<id>/recheck-duplicates/
POST   /api/invoices/<id>/dismiss-duplicate/
POST   /api/invoices/reprocess-failed/   # bulk re-queue failed invoices
POST   /api/invoices/bulk-action/        # bulk export or delete
GET    /api/invoices/usage/              # { invoice_count, invoice_limit, remaining }
GET    /api/invoices/dashboard/?range=30d
GET    /api/notifications/
POST   /api/notifications/mark-read/
```

**Organizations**
```
GET/POST            /api/orgs/
GET/PATCH/DELETE    /api/orgs/<org_id>/
GET                 /api/orgs/<org_id>/members/
GET/PATCH/DELETE    /api/orgs/<org_id>/members/<member_id>/
POST                /api/orgs/<org_id>/invitations/
GET/DELETE          /api/orgs/<org_id>/invitations/<inv_id>/
POST                /api/orgs/<org_id>/invitations/<inv_id>/resend/
POST                /api/orgs/<org_id>/transfer-ownership/
GET                 /api/orgs/<org_id>/activity-log/
GET/POST            /api/invitations/<token>/   # public — accept invite
```

**Gmail**
```
GET    /api/gmail/auth/                  # returns consent URL
GET    /api/gmail/callback/              # OAuth redirect handler (unauthenticated)
GET    /api/gmail/status/
POST   /api/gmail/sync/                  # manual sync trigger
DELETE /api/gmail/disconnect/
GET    /api/gmail/message/<message_id>/
GET    /api/gmail/attachment/?mid=&aid=  # streams attachment bytes
POST   /api/gmail/retry/<synced_msg_id>/
GET    /api/gmail/watch/
POST   /api/gmail/pubsub/               # Pub/Sub push endpoint (unauthenticated)
```

**Billing**
```
POST   /api/billing/create-checkout-session/
POST   /api/billing/verify-checkout/
GET    /api/billing/status/
POST   /api/billing/portal/
POST   /api/billing/webhook/            # Stripe webhook (unauthenticated)
```

**WebSocket**
```
WS     /ws/invoices/?token=<access_token>
```

## Invoice Processing Pipeline

When a file is uploaded (or synced from Gmail), the following happens asynchronously:

1. **`process_invoice` task** — Sends the file to the Mindee V2 API using a custom-trained model. Mindee returns structured field data (vendor, invoice number, date, total, line items) with per-field confidence scores. Results are written to `invoice.extracted_data`. A WebSocket message and in-app notification are pushed to the uploading user.

2. **`check_invoice_duplicates` task** — Runs after extraction. Compares the new invoice against all other processed invoices in the same organization using a staged pipeline:

   - **Rule-based** — Exact or near-exact match on invoice number (0.5), vendor name (0.2), total amount (0.2), and date (0.1). Fast, no external calls.
   - **Fuzzy** — Token-sorted ratio via RapidFuzz on the same four fields with adjusted weights. Catches OCR noise and formatting differences.
   - **Embedding-based** — If `OPEN_AI_API_KEY` is set and the combined rule+fuzzy score clears a minimum threshold (0.15), candidate invoices are embedded with `text-embedding-3-small` and ranked by cosine similarity. Only candidates that score high enough proceed to this stage, keeping API costs proportional to actual ambiguity.
   - **LLM verification** — When the combined score lands in the gray zone (0.40–0.90), both invoices' extracted fields are sent to GPT-4o-mini with a structured JSON schema. The model decides whether it's a true duplicate or a false positive and adjusts the score accordingly.

   Final thresholds: ≥ 0.85 → `DUPLICATE`, ≥ 0.55 → `POSSIBLE_DUPLICATE`, < 0.55 → `UNIQUE`. Full score breakdown is stored in `DuplicateCheckResult` for debugging.

Skipping OpenAI is supported — the system falls back to rule-based + fuzzy only, which is still effective for exact and near-exact duplicates.

## Real-time Updates

`InvoiceConsumer` (Django Channels) handles WebSocket connections. Each authenticated user joins a channel group named `invoices_<user_id>`. Celery tasks use `async_to_sync(channel_layer.group_send())` to broadcast invoice updates and notifications into that group.

Two message types are sent over the socket:

```json
{ "_type": "invoice", "id": 1, "status": "PROCESSED", "extracted_data": {}, "duplicate_check": {} }
{ "_type": "notification", "id": 42, "kind": "INVOICE_PROCESSED", "title": "...", "invoice_id": 1 }
```

Requires Daphne (ASGI) and Redis (Channels layer). `python manage.py runserver` does not support WebSockets.

## Gmail Integration

The Gmail integration uses OAuth 2.0 with `gmail.readonly`, `userinfo.email`, and `openid` scopes. The OAuth state parameter is signed with Django's signing module to prevent CSRF on the callback.

After authorization, syncing works two ways:

- **Polling** — Celery Beat runs `sync_gmail_invoices` every 2 minutes. The first run does a full scan of recent messages; subsequent runs use the Gmail History API to fetch only new messages since the last `history_id`. This keeps the polling lightweight.
- **Push** — If `GMAIL_PUBSUB_TOPIC` is configured, a Gmail watch is registered on connect and renewed every 12 hours. Google pushes history change notifications to `/api/gmail/pubsub/`, which triggers an immediate sync.

Attachment handling filters by MIME type and extension, skips files over 10 MB, and applies keyword heuristics to the filename, subject line, and sender address to identify likely invoices. Processed attachments are tracked in `GmailSyncedMessage` by `(message_id, attachment_id)` to prevent duplicate invoice creation across sync runs.

## Organizations and Roles

All invoice queries are scoped to the requesting user's organization. `OrgScopedMixin` resolves the organization from the `X-Organization-Id` header and attaches it to the request. Roles are: `owner`, `admin`, `member`, `viewer`. The `can_approve` flag on `Membership` is independent of role and controls access to the approval queue.

Plan limits (`free`, `pro`, `business`) are enforced at upload time and during Gmail sync. If the monthly invoice count exceeds the plan limit, uploads return 403 and Gmail sync pauses.

## Billing

Three plans are defined: Free (50 invoices/month, 1 seat), Pro (500/month, 5 seats), Business (unlimited). Stripe Checkout is used for upgrades. Webhooks handle plan state: `checkout.session.completed` activates Pro, `customer.subscription.deleted` downgrades to Free. The `BillingStatusView` returns current plan, usage, and a Stripe customer portal link.

## Production Notes

- **Database** — The default config uses SQLite. Switch to PostgreSQL for any real deployment by setting `DATABASE_URL` and updating `DATABASES` in `settings.py`.
- **Static files** — Run `python manage.py collectstatic` and serve the output directory via a CDN or Nginx. Django's dev static file serving is not suitable for production.
- **HTTPS** — Set `SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")` and `USE_TLS = True` when running behind a reverse proxy.
- **Celery** — Use a process supervisor (systemd, supervisord) to keep the worker and beat scheduler alive. Both must be running for invoice processing and Gmail sync to work.

A minimal Docker Compose setup would look like:

```yaml
services:
  redis:
    image: redis:7-alpine

  web:
    build: .
    command: daphne -b 0.0.0.0 -p 8000 config.asgi:application
    working_dir: /app/src
    depends_on: [redis]
    ports: ["8000:8000"]
    env_file: .env

  worker:
    build: .
    command: celery -A config worker -l info
    working_dir: /app/src
    depends_on: [redis]
    env_file: .env

  beat:
    build: .
    command: celery -A config beat -l info --scheduler django_celery_beat.schedulers:DatabaseScheduler
    working_dir: /app/src
    depends_on: [redis]
    env_file: .env
```
