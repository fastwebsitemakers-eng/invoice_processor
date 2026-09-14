# InvoicePilot AI

**Catch invoice problems before they cost you money.**

AI-assisted invoice validation and anomaly detection for small/mid-sized businesses. This is a lean, revenue-first MVP: a modular Python monolith built to support the first ~1,000 customers without unnecessary infrastructure.

---

## Using this codebase as a template for a different product

Branding (product name, tagline, logo, support email) is a database-backed setting, not hardcoded in templates, so you can reuse this whole app for a different SaaS idea without hunting through HTML files:

- **Quick fork (zero DB edits):** set `APP_NAME`, `APP_TAGLINE`, and `APP_SUPPORT_EMAIL` in `.env` before first run. A fresh install picks these up automatically as the defaults.
- **Change it anytime without redeploying:** log in as a platform admin (`is_platform_admin=True` — the seeded `admin@example.com` account has this) and go to **Admin → Branding**. You can rename the product, edit the tagline, upload a logo (PNG/JPG/SVG, replaces the text logo everywhere), set a support email, or reset back to the `.env` defaults.
- This updates the sidebar, the landing page hero/nav/footer, the login/register pages, and the browser tab title — everywhere the brand shows up. The invoice-validation domain logic (rules, risk scoring, plans) is unrelated to branding, so renaming the product doesn't touch any of that.

If you're forking this for a genuinely different *product* (not just a re-skin — e.g. expense auditing instead of invoice review), the natural places to start are `app/models.py` (entities), `app/services.py` (`ValidationEngine`, business rules), and the `templates/` matching those entities. The auth, multi-tenancy, billing, and branding scaffolding underneath stays as-is.

---

## Architecture

```
Browser
  |
  v
FastAPI routes (app/main.py)   -- thin, no business logic
  |
  v
Services (app/services.py)     -- ValidationEngine, AIProvider, BillingProvider
  |
  v
SQLAlchemy models (app/models.py)
  |
  v
SQLite (dev) / Postgres (prod, via DATABASE_URL)
```

Key design decisions, and why:

- **SQLite by default, Postgres via `DATABASE_URL`.** No Postgres container needed to try the product locally. Swap the env var when you actually need concurrent writers.
- **Synchronous invoice processing, no Celery/Redis.** Validation + mock AI analysis complete in milliseconds. Add a queue when a real AI provider or OCR step makes uploads slow enough to matter — not before.
- **`AIProvider` abstraction** (`app/services.py`): the app ships with `MockAIProvider`, a deterministic rule-based analyzer. Swap in an OpenAI-compatible or Ollama-backed implementation by adding a class with the same `analyze_invoice()` signature — no caller changes.
- **`BillingProvider` abstraction**: `MockBillingProvider` simulates plan changes with no real charge (used automatically when `STRIPE_SECRET_KEY` is unset). `StripeBillingProvider` is a full implementation using real Stripe Checkout (for upgrades) and the Stripe Customer Portal (for plan switches, cancellation, and payment method updates) — see "Accepting real payments with Stripe" below.
- **Multi-tenancy**: every row that matters (`Invoice`, `Vendor`, `AuditLog`, etc.) has an `organization_id`. Every route filters by the logged-in user's org — verified in testing that org A cannot read org B's invoices.
- **AI is advisory only.** Findings and risk scores are computed by a deterministic rules engine (`ValidationEngine`); the AI layer explains them in plain English. It never auto-approves or auto-rejects.

---

## Running locally

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # edit SECRET_KEY before anything real touches this
python -m app.seed             # creates demo org, users, vendors, invoices
uvicorn app.main:app --reload --port 8000
```

Visit http://localhost:8000

**Demo login:** `admin@example.com` / `ChangeMe123!`
*(Development credentials only — change the password immediately in any real deployment.)*

---

## Environment variables

| Variable | Purpose |
|---|---|
| `DATABASE_URL` | `sqlite:///./invoicepilot.db` locally; `postgresql+psycopg://...` in production |
| `SECRET_KEY` | Signs session cookies. Set a long random value in production. |
| `STORAGE_PATH` | Where uploaded invoice files are written. Point at a mounted volume or swap the storage layer for S3 later. |
| `AI_PROVIDER`, `AI_API_KEY`, `AI_MODEL` | Reserved for a real AI provider. Leave blank to use the built-in mock analyzer. |
| `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET`, `STRIPE_PRICE_STARTER`, `STRIPE_PRICE_PROFESSIONAL`, `STRIPE_PRICE_BUSINESS` | Enable real payments. Leave `STRIPE_SECRET_KEY` blank to use the mock billing provider (no real charges). See "Accepting real payments with Stripe" below. |

Never commit `.env`.

---

## Accepting real payments with Stripe

Out of the box, upgrading a plan uses a mock provider — no card is charged. To accept real payments:

1. **Create products and prices.** In the Stripe Dashboard → Product catalog, create three recurring products matching the plans: Starter ($49/mo), Professional ($149/mo), Business ($399/mo). Copy each price's ID (`price_...`).
2. **Set environment variables** in `.env`:
   ```
   STRIPE_SECRET_KEY=sk_test_...
   STRIPE_PRICE_STARTER=price_...
   STRIPE_PRICE_PROFESSIONAL=price_...
   STRIPE_PRICE_BUSINESS=price_...
   ```
3. **Forward webhooks for local testing** with the [Stripe CLI](https://stripe.com/docs/stripe-cli):
   ```
   stripe listen --forward-to localhost:8000/billing/webhook
   ```
   Copy the printed `whsec_...` signing secret into `STRIPE_WEBHOOK_SECRET`. In production, add a webhook endpoint in the Dashboard pointing at `https://yourdomain.com/billing/webhook` (subscribe to `checkout.session.completed`, `customer.subscription.updated`, `customer.subscription.deleted`, `invoice.payment_failed`) and use the signing secret shown there instead.
4. **Restart the app.** With `STRIPE_SECRET_KEY` set, the Billing page automatically switches from the mock buttons to:
   - **"Pay & upgrade"** on Free → opens a real Stripe Checkout session (hosted, PCI-compliant payment page — the actual "enter your card and pay" flow, with Apple Pay/Google Pay support).
   - **"Manage billing"** / **"Switch via billing portal"** on any paid plan → opens the Stripe Customer Portal, where the customer can change plans, update their card, view invoices, or cancel — all hosted by Stripe, so this app never touches raw card data.
   - The `/billing/upgrade` mock route refuses to run once Stripe is configured, so there's no way to bypass real payment once it's live.

**Testing note:** this was built in a sandboxed environment with restricted network egress that can't reach `api.stripe.com`. I verified the integration by (a) confirming the app correctly attempts a real Stripe API call and only fails at the network boundary, (b) offline-verifying the webhook signature logic against a real Stripe-style HMAC signature, and (c) directly testing `apply_stripe_event` (the webhook handler's DB logic) against realistic Stripe payloads — checkout completion, plan switches by price ID, and subscription cancellation all correctly update `Organization.plan` / `Organization.subscription_status`. Do one end-to-end pass with real Stripe test-mode keys before going live.

---

## The revenue loop this app is built around

```
Landing page → Start Free → Create account (org auto-created)
   → Upload invoice → See risk score + findings → See Potential Savings
   → Keep using it → Hit the 25/month Free limit
   → Redirected to Billing with the limit-reached message → Upgrade
   → Recurring revenue
```

This loop is implemented and tested, including the plan-limit wall (verified: invoice #26 on the Free plan is blocked and redirected to `/billing?limit_reached=1`).

Plans (enforced in `app/models.py::PLAN_LIMITS`):

| Plan | Price | Invoices/mo | Users |
|---|---|---|---|
| Free | $0 | 25 | 1 |
| Starter | $49 | 500 | 3 |
| Professional | $149 | 2,500 | 10 |
| Business | $399 | 10,000 | 25 |

---

## Full CRUD coverage

Every core entity now supports Create, Read, Update, and Delete through the UI:

| Entity | Create | Read | Update | Delete |
|---|---|---|---|---|
| Invoices | Upload | List + detail | Edit fields (re-validates + rescoring) | Delete |
| Vendors | Add | List | Edit (incl. active/inactive status) | Delete |
| Users | Invite (Settings → Users) | List | Edit name/role (Settings → Users → Edit) | Delete, or deactivate/activate |
| Validation Rules | Add custom amount-threshold rule | List (Settings → Validation Rules) | Edit severity/threshold, enable/disable | Delete (custom rules) |
| Notifications | Auto-created when an invoice needs review | List | Mark read / mark all read | Delete |
| Organization | Created at signup | Settings page | Edit name/email | — |
| Branding (site) | Auto-created on first run (from `.env` defaults) | Admin → Branding page | Edit name/tagline/logo/support email | Reset to defaults |

A few things worth knowing about how these interact:

- **Validation rule severity actually changes risk scoring** — it isn't cosmetic. Disabling the "Inactive vendor" rule, for example, removes that finding and lowers the score; raising a rule from `medium` to `high` increases its score contribution (low=+10, medium=+20, high=+35).
- **Editing an invoice re-runs validation** — change the vendor, amounts, or dates and the risk score, findings, and AI summary all recompute against the new values.
- **User management respects plan limits and account safety**: you can't add users past your plan's user cap (upgrade prompt shown instead), can't deactivate or delete your own account, and can't delete the last remaining `owner` in an organization.
- **Notifications are created automatically** for org owners/admins whenever an invoice is flagged for review — this is what the Notifications page and CRUD actions operate on.

---

## What's deliberately not included yet

This is scoped as a nimble MVP, not the full enterprise spec. Left out on purpose, with a clear path to add later:

- **Postgres / Redis / Celery** — swap `DATABASE_URL`, and introduce a queue only once background AI/OCR calls are slow enough to justify it.
- **Docker / docker-compose** — not needed to run a single-process app during early development; add once you're deploying to a team or cloud environment.
- **Alembic migrations** — `Base.metadata.create_all()` is fine pre-launch when the schema is still moving fast; introduce Alembic once you have a production database you can't just drop and recreate.
- **Full pytest suite** — the core business logic (`ValidationEngine`, plan limits, tenant isolation) was smoke-tested manually end-to-end during development; add automated regression tests before your first real customer's data goes through it.
- **Real AI provider wiring** — the `AIProvider` interface is ready; swapping in a real LLM (OpenAI-compatible or Ollama) for real invoice OCR/extraction is a scoped, independent task. (Real payments via Stripe are already wired in — see above.)

---

## Security notes

- Passwords hashed with bcrypt, never stored in plaintext.
- Sessions are signed (itsdangerous), httponly, samesite=lax cookies.
- File uploads validated by extension (PDF/PNG/JPG only), size-capped at 10MB, stored under a generated filename (never the user-supplied one), keyed by org.
- Every invoice/vendor/user query is filtered by `organization_id` from the authenticated session — never from client input.
- Admin (`/admin`) routes require `is_platform_admin`; regular org admins cannot reach them.

This is a solid starting posture, not a completed security audit — get a real review before processing real financial documents at scale.
