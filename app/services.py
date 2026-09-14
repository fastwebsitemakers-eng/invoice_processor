"""
Business logic layer. Routes should never contain this logic directly -
they call into these services.
"""
import os
import hashlib
from abc import ABC, abstractmethod
from datetime import date, datetime
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    Invoice, Vendor, ValidationResult, ValidationRule, AuditLog, Organization,
    Subscription, Notification, SiteSettings, Page, ContentBlock, User, PLAN_LIMITS,
)

# --------------------------------------------------------------- risk ------

# Default weight contributed by each severity level. A rule's *severity* is
# user-editable (Settings > Validation Rules), so changing it changes the
# invoice's risk score - this is the "configurable scoring engine" from the
# product spec, not just cosmetic labels.
SEVERITY_WEIGHTS = {"low": 10, "medium": 20, "high": 35}

# Built-in rule catalog. Every organization gets one ValidationRule row per
# entry below (see ensure_default_rules) so they show up in Settings and can
# be toggled on/off or have their severity edited. If a row is missing for a
# given rule_type (e.g. it was deleted), the engine falls back to this
# default severity and treats the rule as active.
DEFAULT_RULES = [
    ("duplicate_invoice_number", "Duplicate invoice number",
     "Flags invoices reusing a number already on file for the same vendor.", "high"),
    ("duplicate_document", "Duplicate document",
     "Flags when the exact same uploaded file has been seen before.", "high"),
    ("missing_po", "Missing PO number",
     "Flags invoices with no purchase order reference.", "low"),
    ("missing_vendor", "Missing vendor",
     "Flags invoices with no vendor selected.", "medium"),
    ("unknown_vendor", "Inactive vendor",
     "Flags invoices billed to a vendor marked inactive.", "high"),
    ("missing_invoice_date", "Missing invoice date",
     "Flags invoices with no invoice date.", "low"),
    ("past_due", "Past due",
     "Flags unpaid invoices already past their due date.", "medium"),
    ("tax_mismatch", "Subtotal/tax mismatch",
     "Flags when subtotal + tax doesn't match the stated total.", "medium"),
    ("amount_anomaly", "Unusual amount",
     "Flags invoices more than 3x a vendor's historical average.", "high"),
]


def ensure_default_rules(db: Session, organization_id: str) -> None:
    """Idempotently creates the built-in rule rows for an organization so
    they're visible and editable under Settings > Validation Rules."""
    existing_types = set(db.scalars(
        select(ValidationRule.rule_type).where(ValidationRule.organization_id == organization_id)
    ).all())
    for rule_type, name, description, severity in DEFAULT_RULES:
        if rule_type in existing_types:
            continue
        db.add(ValidationRule(
            organization_id=organization_id, name=name, description=description,
            rule_type=rule_type, severity=severity, is_active=True, configuration_json={},
        ))


def risk_level_for(score: int) -> str:
    if score >= 80:
        return "critical"
    if score >= 60:
        return "high"
    if score >= 30:
        return "medium"
    return "low"


class ValidationEngine:
    """Runs configurable business rules against an invoice and produces
    ValidationResult rows plus a deterministic, explainable risk score.
    Rule on/off state and severity come from the organization's
    ValidationRule rows, so editing a rule in Settings changes real
    behavior, not just a label."""

    def __init__(self, db: Session, organization_id: str):
        self.db = db
        self.organization_id = organization_id
        self.rules: dict[str, ValidationRule] = {
            r.rule_type: r for r in db.scalars(
                select(ValidationRule).where(ValidationRule.organization_id == organization_id)
            ).all()
        }

    def _active(self, rule_type: str) -> bool:
        rule = self.rules.get(rule_type)
        return rule.is_active if rule else True

    def _severity(self, rule_type: str, default: str) -> str:
        rule = self.rules.get(rule_type)
        return rule.severity if rule and rule.severity in SEVERITY_WEIGHTS else default

    def run(self, invoice: Invoice) -> tuple[list[ValidationResult], int]:
        findings: list[ValidationResult] = []
        score = 0

        def add(rule_type: str, default_severity: str, message: str):
            nonlocal score
            if not self._active(rule_type):
                return
            severity = self._severity(rule_type, default_severity)
            score += SEVERITY_WEIGHTS[severity]
            findings.append(ValidationResult(
                rule_type=rule_type, severity=severity, message=message, status="failed"
            ))

        # Duplicate invoice number for same vendor
        if invoice.invoice_number:
            dup = self.db.scalar(
                select(func.count(Invoice.id)).where(
                    Invoice.organization_id == self.organization_id,
                    Invoice.invoice_number == invoice.invoice_number,
                    Invoice.vendor_id == invoice.vendor_id,
                    Invoice.id != invoice.id,
                )
            )
            if dup:
                add("duplicate_invoice_number", "high",
                    f"Invoice number '{invoice.invoice_number}' was already used for this vendor.")

        # Duplicate document hash (same file uploaded twice)
        if invoice.document_hash:
            dup_doc = self.db.scalar(
                select(func.count(Invoice.id)).where(
                    Invoice.organization_id == self.organization_id,
                    Invoice.document_hash == invoice.document_hash,
                    Invoice.id != invoice.id,
                )
            )
            if dup_doc:
                add("duplicate_document", "high", "This exact document has been uploaded before.")

        if not invoice.po_number:
            add("missing_po", "low", "No purchase order number was provided.")

        if not invoice.vendor_id:
            add("missing_vendor", "medium", "Invoice has no vendor on file.")
        else:
            vendor = self.db.get(Vendor, invoice.vendor_id)
            if vendor and vendor.status != "active":
                add("unknown_vendor", "high", f"Vendor '{vendor.name}' is not active.")

        if not invoice.invoice_date:
            add("missing_invoice_date", "low", "Invoice date is missing.")

        if invoice.due_date and invoice.due_date < date.today() and invoice.status not in ("paid",):
            add("past_due", "medium", f"Invoice was due {invoice.due_date} and is unpaid.")

        expected_total = round((invoice.subtotal or 0) + (invoice.tax or 0), 2)
        if invoice.total and abs(expected_total - invoice.total) > 0.01 and expected_total > 0:
            add("tax_mismatch", "medium",
                f"Subtotal + tax (${expected_total:,.2f}) does not match total (${invoice.total:,.2f}).")

        # Amount anomaly vs vendor's historical average
        if invoice.vendor_id and invoice.total:
            avg_total = self.db.scalar(
                select(func.avg(Invoice.total)).where(
                    Invoice.organization_id == self.organization_id,
                    Invoice.vendor_id == invoice.vendor_id,
                    Invoice.id != invoice.id,
                )
            )
            if avg_total and avg_total > 0 and invoice.total > avg_total * 3:
                add("amount_anomaly", "high",
                    f"Amount (${invoice.total:,.2f}) is more than 3x this vendor's average (${avg_total:,.2f}).")

        # Custom, user-defined amount-threshold rules (Settings > Validation Rules)
        for rule in self.rules.values():
            if rule.rule_type != "amount_threshold" or not rule.is_active:
                continue
            threshold = (rule.configuration_json or {}).get("threshold")
            if threshold and invoice.total and invoice.total >= float(threshold):
                score += SEVERITY_WEIGHTS.get(rule.severity, SEVERITY_WEIGHTS["medium"])
                findings.append(ValidationResult(
                    rule_type="amount_threshold", severity=rule.severity, status="failed",
                    message=f"Invoice total (${invoice.total:,.2f}) is at or above the configured "
                            f"threshold of ${float(threshold):,.2f} ('{rule.name}').",
                ))

        score = min(score, 100)
        return findings, score


# --------------------------------------------------------------- AI --------

class AIProvider(ABC):
    """Abstraction so the business logic never hard-codes a specific AI
    vendor. Swap MockAIProvider for an OpenAI-compatible or Ollama-backed
    implementation without touching callers."""

    @abstractmethod
    def analyze_invoice(self, invoice: Invoice, findings: list[ValidationResult], risk_score: int) -> dict:
        ...


class MockAIProvider(AIProvider):
    """Deterministic, explainable stand-in for a real LLM call. Produces the
    same output shape a real provider would (risk_level, summary,
    recommendations) so swapping in a real model is a drop-in change."""

    def analyze_invoice(self, invoice: Invoice, findings: list[ValidationResult], risk_score: int) -> dict:
        level = risk_level_for(risk_score)
        if not findings:
            summary = "No anomalies detected. Invoice looks consistent with prior history."
            recs = ["No action needed. Safe to route for standard approval."]
        else:
            top = sorted(findings, key=lambda f: {"high": 0, "medium": 1, "low": 2}[f.severity])[:3]
            summary = f"Invoice has {len(findings)} issue(s), the most notable being: " + \
                       "; ".join(f.message for f in top)
            recs = [f"Review: {f.message}" for f in top]
            if level in ("high", "critical"):
                recs.append("Recommend manual review before approval.")
        return {
            "risk_score": risk_score,
            "risk_level": level,
            "summary": summary,
            "findings": [{"type": f.rule_type, "severity": f.severity, "description": f.message} for f in findings],
            "recommendations": recs,
        }


def get_ai_provider() -> AIProvider:
    provider = os.environ.get("AI_PROVIDER", "mock")
    # Real providers (OpenAI-compatible, Ollama, etc.) would be registered
    # here and selected by AI_PROVIDER / AI_API_KEY without changing callers.
    return MockAIProvider()


# ----------------------------------------------------------- billing -------

# Which env var holds the Stripe Price ID for each paid plan. Set these in
# the Stripe Dashboard (Product catalog) and copy the price IDs into .env.
# Free has no Stripe price - there's nothing to check out for $0/mo.
STRIPE_PRICE_ENV_VARS = {
    "starter": "STRIPE_PRICE_STARTER",
    "professional": "STRIPE_PRICE_PROFESSIONAL",
    "business": "STRIPE_PRICE_BUSINESS",
}

STRIPE_ENABLED = bool(os.environ.get("STRIPE_SECRET_KEY"))


class BillingProvider(ABC):
    @abstractmethod
    def create_customer(self, org: Organization) -> str: ...

    @abstractmethod
    def create_subscription(self, org: Organization, plan: str) -> dict: ...

    @abstractmethod
    def change_plan(self, org: Organization, plan: str) -> dict: ...

    @abstractmethod
    def cancel_subscription(self, org: Organization) -> dict: ...


class MockBillingProvider(BillingProvider):
    """Stands in for Stripe when no STRIPE_SECRET_KEY is configured (e.g.
    local development). Same method surface Stripe uses, so nothing else
    in the app needs to know which provider is active."""

    def create_customer(self, org: Organization) -> str:
        return f"mock_cus_{org.id[:12]}"

    def create_subscription(self, org: Organization, plan: str) -> dict:
        return {"status": "active", "external_subscription_id": f"mock_sub_{org.id[:12]}", "plan": plan}

    def change_plan(self, org: Organization, plan: str) -> dict:
        return {"status": "active", "plan": plan}

    def cancel_subscription(self, org: Organization) -> dict:
        return {"status": "canceled"}


class StripeBillingProvider(BillingProvider):
    """Real Stripe integration. Upgrades go through Stripe Checkout (a
    hosted, PCI-compliant payment page - the actual 'enter your card and
    pay' flow). Plan switches, cancellations, payment method updates, and
    invoice history are all handled through the Stripe Customer Portal
    rather than reimplemented here, which is the pattern Stripe recommends
    so this app never touches raw card data.

    create_subscription/change_plan/cancel_subscription still satisfy the
    BillingProvider interface (e.g. for admin-initiated or scripted
    changes), but the everyday user flow is checkout_url / portal_url
    below.
    """

    def __init__(self):
        import stripe  # imported lazily so the package is only required when Stripe is actually enabled
        self.stripe = stripe
        self.stripe.api_key = os.environ["STRIPE_SECRET_KEY"]

    def _price_id_for(self, plan: str) -> str:
        env_var = STRIPE_PRICE_ENV_VARS.get(plan)
        price_id = env_var and os.environ.get(env_var)
        if not price_id:
            raise ValueError(
                f"No Stripe Price ID configured for plan '{plan}'. "
                f"Set {env_var or '(unknown plan)'} in your environment."
            )
        return price_id

    def create_customer(self, org: Organization) -> str:
        customer = self.stripe.Customer.create(
            email=org.email, name=org.name, metadata={"organization_id": org.id},
        )
        return customer.id

    def _customer_id_for(self, db: Session, org: Organization) -> str:
        sub = db.scalar(select(Subscription).where(Subscription.organization_id == org.id))
        if sub and sub.external_customer_id:
            return sub.external_customer_id
        customer_id = self.create_customer(org)
        if sub:
            sub.external_customer_id = customer_id
        else:
            db.add(Subscription(organization_id=org.id, provider="stripe",
                                 external_customer_id=customer_id, plan=org.plan, status="incomplete"))
        db.flush()
        return customer_id

    def create_checkout_session(self, db: Session, org: Organization, plan: str,
                                 success_url: str, cancel_url: str) -> str:
        """Creates a Stripe Checkout Session - the hosted page where the
        user actually enters payment details and pays. Returns the URL to
        redirect the browser to."""
        customer_id = self._customer_id_for(db, org)
        session = self.stripe.checkout.Session.create(
            mode="subscription",
            customer=customer_id,
            line_items=[{"price": self._price_id_for(plan), "quantity": 1}],
            success_url=success_url,
            cancel_url=cancel_url,
            client_reference_id=org.id,
            subscription_data={"metadata": {"organization_id": org.id, "plan": plan}},
            metadata={"organization_id": org.id, "plan": plan},
            allow_promotion_codes=True,
        )
        return session.url

    def create_portal_session(self, db: Session, org: Organization, return_url: str) -> str:
        """Creates a Stripe Customer Portal session where the user can
        change plans, update their card, view invoices, or cancel."""
        customer_id = self._customer_id_for(db, org)
        session = self.stripe.billing_portal.Session.create(customer=customer_id, return_url=return_url)
        return session.url

    def create_subscription(self, org: Organization, plan: str) -> dict:
        # Everyday upgrades go through create_checkout_session (above).
        # This exists to satisfy the interface for non-interactive use.
        raise NotImplementedError("Use create_checkout_session for user-facing upgrades.")

    def change_plan(self, org: Organization, plan: str) -> dict:
        raise NotImplementedError("Plan changes go through the Stripe Customer Portal (create_portal_session).")

    def cancel_subscription(self, org: Organization) -> dict:
        raise NotImplementedError("Cancellation goes through the Stripe Customer Portal (create_portal_session).")


def get_billing_provider() -> BillingProvider:
    if STRIPE_ENABLED:
        return StripeBillingProvider()
    return MockBillingProvider()


def _plan_for_price_id(price_id: str) -> str | None:
    for plan, env_var in STRIPE_PRICE_ENV_VARS.items():
        if os.environ.get(env_var) == price_id:
            return plan
    return None


def _get_or_create_subscription_row(db: Session, organization_id: str) -> Subscription:
    sub = db.scalar(select(Subscription).where(Subscription.organization_id == organization_id))
    if not sub:
        sub = Subscription(organization_id=organization_id, provider="stripe")
        db.add(sub)
        db.flush()
    return sub


def apply_stripe_event(db: Session, event_type: str, data_object: dict) -> None:
    """Applies a verified Stripe webhook event to local state. Called from
    the /billing/webhook route after signature verification. Kept here
    (not in main.py) so the route stays a thin HTTP adapter."""

    if event_type == "checkout.session.completed":
        if data_object.get("mode") != "subscription":
            return
        org_id = (data_object.get("metadata") or {}).get("organization_id") or data_object.get("client_reference_id")
        plan = (data_object.get("metadata") or {}).get("plan")
        if not org_id or not plan:
            return
        org = db.get(Organization, org_id)
        if not org:
            return
        org.plan = plan
        org.subscription_status = "active"
        sub = _get_or_create_subscription_row(db, org.id)
        sub.provider = "stripe"
        sub.plan = plan
        sub.status = "active"
        sub.external_customer_id = data_object.get("customer") or sub.external_customer_id
        sub.external_subscription_id = data_object.get("subscription") or sub.external_subscription_id
        log_audit(db, org.id, None, "subscription.changed", "organization", org.id, f"stripe checkout -> {plan}")

    elif event_type in ("customer.subscription.updated", "customer.subscription.created"):
        sub_id = data_object.get("id")
        org_id = (data_object.get("metadata") or {}).get("organization_id")
        sub = None
        if sub_id:
            sub = db.scalar(select(Subscription).where(Subscription.external_subscription_id == sub_id))
        if not sub and org_id:
            sub = _get_or_create_subscription_row(db, org_id)
            sub.external_subscription_id = sub_id
        if not sub:
            return
        org = db.get(Organization, sub.organization_id)
        if not org:
            return
        items = (data_object.get("items") or {}).get("data") or []
        price_id = items[0]["price"]["id"] if items and items[0].get("price") else None
        resolved_plan = _plan_for_price_id(price_id) if price_id else None
        status = data_object.get("status", sub.status)
        sub.status = status
        if resolved_plan:
            sub.plan = resolved_plan
        if status in ("active", "trialing"):
            org.subscription_status = "active"
            if resolved_plan:
                org.plan = resolved_plan
        elif status in ("past_due", "unpaid", "incomplete", "incomplete_expired"):
            org.subscription_status = status
        log_audit(db, org.id, None, "subscription.changed", "organization", org.id, f"stripe status={status}")

    elif event_type == "customer.subscription.deleted":
        sub_id = data_object.get("id")
        sub = db.scalar(select(Subscription).where(Subscription.external_subscription_id == sub_id)) if sub_id else None
        if not sub:
            return
        org = db.get(Organization, sub.organization_id)
        if not org:
            return
        sub.status = "canceled"
        sub.plan = "free"
        org.plan = "free"
        org.subscription_status = "canceled"
        log_audit(db, org.id, None, "subscription.changed", "organization", org.id, "stripe subscription canceled -> free")

    elif event_type == "invoice.payment_failed":
        sub_id = data_object.get("subscription")
        sub = db.scalar(select(Subscription).where(Subscription.external_subscription_id == sub_id)) if sub_id else None
        if not sub:
            return
        org = db.get(Organization, sub.organization_id)
        if org:
            org.subscription_status = "past_due"
            log_audit(db, org.id, None, "subscription.changed", "organization", org.id, "stripe payment failed")


# ------------------------------------------------------------ plan use -----

def invoices_used_this_month(db: Session, organization_id: str) -> int:
    start_of_month = datetime.utcnow().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return db.scalar(
        select(func.count(Invoice.id)).where(
            Invoice.organization_id == organization_id,
            Invoice.created_at >= start_of_month,
        )
    ) or 0


def plan_limit_reached(db: Session, org: Organization) -> bool:
    limit = org.limits()["invoices"]
    return invoices_used_this_month(db, org.id) >= limit


def users_used(db: Session, organization_id: str) -> int:
    return db.scalar(
        select(func.count(User.id)).where(User.organization_id == organization_id)
    ) or 0


def user_limit_reached(db: Session, org: Organization) -> bool:
    return users_used(db, org.id) >= org.limits()["users"]


def file_hash(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def log_audit(db: Session, organization_id: str, user_id: str | None, action: str,
              entity_type: str = "", entity_id: str = "", details: str = ""):
    db.add(AuditLog(
        organization_id=organization_id, user_id=user_id, action=action,
        entity_type=entity_type, entity_id=entity_id, details=details,
    ))


# -------------------------------------------------------- notifications ----

def notify_reviewers(db: Session, organization_id: str, invoice: Invoice) -> None:
    """Creates an in-app notification for every owner/admin in the org when
    an invoice is flagged for review."""
    reviewers = db.scalars(
        select(User).where(
            User.organization_id == organization_id,
            User.role.in_(["owner", "admin"]),
            User.is_active.is_(True),
        )
    ).all()
    for reviewer in reviewers:
        db.add(Notification(
            organization_id=organization_id,
            user_id=reviewer.id,
            type="invoice_needs_review",
            title="Invoice needs review",
            message=f"Invoice {invoice.invoice_number or invoice.id[:8]} was flagged "
                    f"{invoice.risk_level} risk ({invoice.risk_score}/100) and needs a decision.",
        ))


# ------------------------------------------------------------- branding ----

# Quick-fork defaults: set these in .env when reusing this codebase for a
# different product so a fresh install already shows the right name/tagline
# without anyone touching the database. The in-app Branding page (platform
# admin only) can then override them per-deployment without redeploying.
DEFAULT_PRODUCT_NAME = os.environ.get("APP_NAME", "InvoicePilot AI")
DEFAULT_TAGLINE = os.environ.get("APP_TAGLINE", "Catch invoice problems before they cost you money.")
DEFAULT_SUPPORT_EMAIL = os.environ.get("APP_SUPPORT_EMAIL", "")


def ensure_site_settings(db: Session) -> SiteSettings:
    """Read (and lazily Create) the singleton branding row."""
    settings = db.get(SiteSettings, "site")
    if not settings:
        settings = SiteSettings(
            id="site", product_name=DEFAULT_PRODUCT_NAME, tagline=DEFAULT_TAGLINE,
            support_email=DEFAULT_SUPPORT_EMAIL, logo_url="",
        )
        db.add(settings)
        db.flush()
    return settings


def site_settings_dict(s: SiteSettings) -> dict:
    """Plain-dict snapshot, safe to hand to a template or stash on
    request.state after the DB session that loaded it has closed."""
    return {
        "product_name": s.product_name,
        "tagline": s.tagline,
        "logo_url": s.logo_url,
        "support_email": s.support_email,
        "term_record_singular": s.term_record_singular,
        "term_record_plural": s.term_record_plural,
        "term_party_singular": s.term_party_singular,
        "term_party_plural": s.term_party_plural,
    }


# --------------------------------------------------------------- CMS -------

# Every page/block route lives under these path segments, or is a fixed
# top-level route already registered elsewhere in main.py. A custom page
# can't use any of these slugs, and neither the "home" nor "pricing" system
# pages can be renamed to collide with them either.
RESERVED_SLUGS = {
    "", "login", "register", "logout", "dashboard", "invoices", "vendors",
    "billing", "notifications", "settings", "admin", "static",
    "branding-assets", "content-assets", "docs", "redoc", "openapi.json",
    "api", "pricing", "home",
}

# block_type -> the data_json keys that are meaningful for it, and which of
# those are required. Drives both the admin add/edit forms and validation.
BLOCK_FIELDS: dict[str, dict[str, bool]] = {
    "hero": {
        "heading": True, "subheading": False,
        "primary_cta_label": False, "primary_cta_url": False,
        "secondary_cta_label": False, "secondary_cta_url": False,
    },
    "section_heading": {"heading": True, "subheading": False},
    "rich_text": {"heading": False, "body": True},
    "feature_item": {"title": True, "description": False},
    "faq_item": {"question": True, "answer": True},
    "cta_banner": {"heading": True, "subtext": False, "button_label": False, "button_url": False},
    "image_text": {"heading": False, "body": False, "image_url": False, "layout": False},
    "pricing_section": {"heading": True, "subheading": False},
    "spacer": {"size": False},
}
BLOCK_TYPES = list(BLOCK_FIELDS.keys())
GROUPED_BLOCK_TYPES = {"feature_item", "faq_item"}
BLOCK_TYPE_LABELS = {
    "hero": "Hero banner", "section_heading": "Section heading", "rich_text": "Text block",
    "feature_item": "Feature / list item", "faq_item": "FAQ item", "cta_banner": "Call-to-action banner",
    "image_text": "Image + text", "pricing_section": "Pricing section (plans shown automatically)",
    "spacer": "Spacer",
}


def is_slug_available(db: Session, slug: str, exclude_page_id: str | None = None) -> bool:
    slug = (slug or "").strip().lower()
    if not slug or slug in RESERVED_SLUGS:
        return False
    if not all(c.isalnum() or c in ("-", "_") for c in slug):
        return False
    existing = db.scalar(select(Page).where(Page.slug == slug))
    if existing and existing.id != exclude_page_id:
        return False
    return True


def _next_block_position(db: Session, page_id: str) -> int:
    max_pos = db.scalar(select(func.max(ContentBlock.position)).where(ContentBlock.page_id == page_id))
    return (max_pos or 0) + 10


def add_block(db: Session, page_id: str, block_type: str, data: dict) -> ContentBlock:
    block = ContentBlock(
        page_id=page_id, block_type=block_type, data_json=data,
        position=_next_block_position(db, page_id), is_active=True,
    )
    db.add(block)
    db.flush()
    return block


def move_block(db: Session, block: ContentBlock, direction: str) -> None:
    """direction: 'up' or 'down'. Swaps position with the nearest neighbor
    in that direction among the same page's blocks."""
    siblings = db.scalars(
        select(ContentBlock).where(ContentBlock.page_id == block.page_id).order_by(ContentBlock.position)
    ).all()
    idx = next((i for i, b in enumerate(siblings) if b.id == block.id), None)
    if idx is None:
        return
    if direction == "up" and idx > 0:
        other = siblings[idx - 1]
    elif direction == "down" and idx < len(siblings) - 1:
        other = siblings[idx + 1]
    else:
        return
    block.position, other.position = other.position, block.position


def group_blocks_for_render(blocks: list[ContentBlock]) -> list[dict]:
    """Turns a flat, ordered list of active blocks into render-ready
    groups: consecutive feature_item blocks become one 'feature_grid'
    group, consecutive faq_item blocks become one 'faq_group', everything
    else renders individually. Keeps the public page template simple."""
    groups: list[dict] = []
    i = 0
    while i < len(blocks):
        b = blocks[i]
        if b.block_type in GROUPED_BLOCK_TYPES:
            run = [b]
            j = i + 1
            while j < len(blocks) and blocks[j].block_type == b.block_type:
                run.append(blocks[j])
                j += 1
            group_kind = "feature_grid" if b.block_type == "feature_item" else "faq_group"
            groups.append({"kind": group_kind, "entries": [x.data_json for x in run]})
            i = j
        else:
            groups.append({"kind": b.block_type, "data": b.data_json, "id": b.id})
            i += 1
    return groups


# Default content seeded onto the "home" and "pricing" system pages on first
# run - reconstructs a reasonable version of the original marketing copy so
# a fresh install doesn't look empty, entirely through the same block
# system a user would edit afterwards. Feel free to rewrite all of it from
# Admin > Pages.
_DEFAULT_HOME_BLOCKS = [
    ("hero", {
        "heading": "Catch invoice problems before they cost you money.",
        "subheading": "AI-powered invoice validation and anomaly detection for growing businesses.",
        "primary_cta_label": "Start Free", "primary_cta_url": "/register",
        "secondary_cta_label": "See How It Works", "secondary_cta_url": "#how",
    }),
    ("section_heading", {"heading": "How it works"}),
    ("feature_item", {"title": "1. Upload", "description": "Drop in a document, or connect your inbox later."}),
    ("feature_item", {"title": "2. Validate", "description": "Rules and AI check for duplicates, anomalies, and missing data."}),
    ("feature_item", {"title": "3. Review", "description": "High-risk items route to a reviewer with a clear explanation."}),
    ("feature_item", {"title": "4. Approve", "description": "Everything else clears automatically, with a full audit trail."}),
    ("section_heading", {"heading": "Everything you need, nothing you don't"}),
    ("feature_item", {"title": "Duplicate detection", "description": "Catches repeated numbers and identical documents automatically."}),
    ("feature_item", {"title": "Configurable rules", "description": "Set thresholds for missing fields, mismatches, and unusual amounts."}),
    ("feature_item", {"title": "AI risk explanations", "description": "Every flagged item comes with a plain-English summary - never an auto-decision."}),
    ("feature_item", {"title": "Vendor analytics", "description": "See spend, exception rate, and risk trends over time."}),
    ("feature_item", {"title": "Full audit trail", "description": "Every approval, rejection, and rule change is logged for compliance."}),
    ("feature_item", {"title": "Role-based access", "description": "Owners, admins, managers, reviewers, and viewers - scoped to your organization only."}),
    ("pricing_section", {"heading": "Simple, usage-based pricing", "subheading": "Start free. Upgrade the moment volume outgrows the plan."}),
    ("section_heading", {"heading": "Frequently asked questions"}),
    ("faq_item", {"question": "Does the AI automatically approve or reject anything?",
                  "answer": "No. AI findings are always advisory - a human makes every decision."}),
    ("faq_item", {"question": "What file types can I upload?", "answer": "PDF, PNG, and JPG, up to 10MB per file."}),
    ("faq_item", {"question": "Can I change plans later?", "answer": "Yes, upgrade or downgrade anytime from Billing."}),
    ("faq_item", {"question": "Is my data isolated from other companies?",
                  "answer": "Yes. Every organization's data is fully isolated at the data layer."}),
    ("cta_banner", {"heading": "Stop paying for mistakes.", "subtext": "Set up in minutes. First 25 items a month, free.",
                     "button_label": "Start Free", "button_url": "/register"}),
]

_DEFAULT_PRICING_BLOCKS = [
    ("pricing_section", {"heading": "Plans & pricing", "subheading": "Pick the plan that fits your volume today - change anytime."}),
]


def ensure_default_pages(db: Session) -> None:
    """Idempotently creates the 'home' and 'pricing' system pages with
    starter content, the first time the app runs against a fresh
    database."""
    if not db.scalar(select(Page).where(Page.slug == "home")):
        home = Page(slug="home", title="Home", is_system=True, is_published=True, show_in_nav=False)
        db.add(home)
        db.flush()
        for block_type, data in _DEFAULT_HOME_BLOCKS:
            add_block(db, home.id, block_type, data)

    if not db.scalar(select(Page).where(Page.slug == "pricing")):
        pricing = Page(slug="pricing", title="Pricing", is_system=True, is_published=True, show_in_nav=False)
        db.add(pricing)
        db.flush()
        for block_type, data in _DEFAULT_PRICING_BLOCKS:
            add_block(db, pricing.id, block_type, data)
