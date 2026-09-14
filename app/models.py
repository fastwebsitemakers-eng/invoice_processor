import enum
import uuid
from datetime import datetime, date
from sqlalchemy import (
    String, Integer, Float, Boolean, DateTime, Date, ForeignKey, Text, JSON
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.db import Base


def gen_id() -> str:
    return uuid.uuid4().hex


# ---------------------------------------------------------------- plans ----

PLAN_LIMITS = {
    "free": {"invoices": 25, "users": 1, "price": 0, "label": "Free"},
    "starter": {"invoices": 500, "users": 3, "price": 49, "label": "Starter"},
    "professional": {"invoices": 2500, "users": 10, "price": 149, "label": "Professional"},
    "business": {"invoices": 10000, "users": 25, "price": 399, "label": "Business"},
}

ROLES = ["owner", "admin", "manager", "reviewer", "viewer"]


class Organization(Base):
    __tablename__ = "organizations"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=gen_id)
    name: Mapped[str] = mapped_column(String, nullable=False)
    slug: Mapped[str] = mapped_column(String, unique=True, index=True)
    email: Mapped[str] = mapped_column(String)
    plan: Mapped[str] = mapped_column(String, default="free")
    subscription_status: Mapped[str] = mapped_column(String, default="active")
    is_suspended: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    users: Mapped[list["User"]] = relationship(back_populates="organization", cascade="all, delete-orphan")
    vendors: Mapped[list["Vendor"]] = relationship(back_populates="organization", cascade="all, delete-orphan")
    invoices: Mapped[list["Invoice"]] = relationship(back_populates="organization", cascade="all, delete-orphan")

    def limits(self):
        return PLAN_LIMITS.get(self.plan, PLAN_LIMITS["free"])


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=gen_id)
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"))
    email: Mapped[str] = mapped_column(String, unique=True, index=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String, nullable=False)
    first_name: Mapped[str] = mapped_column(String, default="")
    last_name: Mapped[str] = mapped_column(String, default="")
    role: Mapped[str] = mapped_column(String, default="owner")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    is_platform_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    organization: Mapped["Organization"] = relationship(back_populates="users")

    @property
    def full_name(self):
        return f"{self.first_name} {self.last_name}".strip() or self.email


class Vendor(Base):
    __tablename__ = "vendors"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=gen_id)
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"))
    name: Mapped[str] = mapped_column(String, nullable=False)
    vendor_number: Mapped[str] = mapped_column(String, default="")
    email: Mapped[str] = mapped_column(String, default="")
    phone: Mapped[str] = mapped_column(String, default="")
    address: Mapped[str] = mapped_column(String, default="")
    tax_id: Mapped[str] = mapped_column(String, default="")
    payment_terms: Mapped[str] = mapped_column(String, default="Net 30")
    status: Mapped[str] = mapped_column(String, default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    organization: Mapped["Organization"] = relationship(back_populates="vendors")
    invoices: Mapped[list["Invoice"]] = relationship(back_populates="vendor")


class Invoice(Base):
    __tablename__ = "invoices"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=gen_id)
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True)
    vendor_id: Mapped[str | None] = mapped_column(ForeignKey("vendors.id"), nullable=True)
    invoice_number: Mapped[str] = mapped_column(String, default="")
    invoice_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    due_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    currency: Mapped[str] = mapped_column(String, default="USD")
    subtotal: Mapped[float] = mapped_column(Float, default=0)
    tax: Mapped[float] = mapped_column(Float, default=0)
    total: Mapped[float] = mapped_column(Float, default=0)
    status: Mapped[str] = mapped_column(String, default="uploaded")
    approval_status: Mapped[str] = mapped_column(String, default="pending")
    source: Mapped[str] = mapped_column(String, default="manual")
    document_path: Mapped[str] = mapped_column(String, default="")
    document_hash: Mapped[str] = mapped_column(String, default="", index=True)
    po_number: Mapped[str] = mapped_column(String, default="")
    risk_score: Mapped[int] = mapped_column(Integer, default=0)
    risk_level: Mapped[str] = mapped_column(String, default="low")
    ai_summary: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    organization: Mapped["Organization"] = relationship(back_populates="invoices")
    vendor: Mapped["Vendor"] = relationship(back_populates="invoices")
    validation_results: Mapped[list["ValidationResult"]] = relationship(
        back_populates="invoice", cascade="all, delete-orphan"
    )


class ValidationRule(Base):
    __tablename__ = "validation_rules"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=gen_id)
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"))
    name: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str] = mapped_column(String, default="")
    rule_type: Mapped[str] = mapped_column(String, nullable=False)
    configuration_json: Mapped[dict] = mapped_column(JSON, default=dict)
    severity: Mapped[str] = mapped_column(String, default="medium")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class ValidationResult(Base):
    __tablename__ = "validation_results"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=gen_id)
    invoice_id: Mapped[str] = mapped_column(ForeignKey("invoices.id"))
    rule_type: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, default="failed")
    severity: Mapped[str] = mapped_column(String, default="medium")
    message: Mapped[str] = mapped_column(String, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    invoice: Mapped["Invoice"] = relationship(back_populates="validation_results")


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=gen_id)
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"))
    user_id: Mapped[str | None] = mapped_column(String, nullable=True)
    action: Mapped[str] = mapped_column(String)
    entity_type: Mapped[str] = mapped_column(String, default="")
    entity_id: Mapped[str] = mapped_column(String, default="")
    details: Mapped[str] = mapped_column(String, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class SiteSettings(Base):
    """Singleton row holding product branding (name, tagline, logo, support
    email) plus domain terminology, so this codebase can be reused for a
    different product without hunting through templates for hardcoded
    strings. Always has exactly one row (id='site') - see
    services.ensure_site_settings.

    Domain terminology relabels the existing invoice/vendor workflow for a
    different business (e.g. "Claim" / "Provider") without renaming the
    underlying Python classes or database tables - those stay as Invoice/
    Vendor internally. term_* fields drive what the UI *displays*.
    """
    __tablename__ = "site_settings"

    id: Mapped[str] = mapped_column(String, primary_key=True, default="site")
    product_name: Mapped[str] = mapped_column(String, default="InvoicePilot AI")
    tagline: Mapped[str] = mapped_column(String, default="Catch invoice problems before they cost you money.")
    logo_url: Mapped[str] = mapped_column(String, default="")
    support_email: Mapped[str] = mapped_column(String, default="")

    term_record_singular: Mapped[str] = mapped_column(String, default="Invoice")
    term_record_plural: Mapped[str] = mapped_column(String, default="Invoices")
    term_party_singular: Mapped[str] = mapped_column(String, default="Vendor")
    term_party_plural: Mapped[str] = mapped_column(String, default="Vendors")

    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class Page(Base):
    """A CMS-managed page. 'home' (slug='home', served at '/') and
    'pricing' (slug='pricing') are system pages created automatically and
    cannot be deleted, but their content blocks are fully editable like any
    other page. Any other page is served at /<slug>."""
    __tablename__ = "pages"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=gen_id)
    slug: Mapped[str] = mapped_column(String, unique=True, index=True)
    title: Mapped[str] = mapped_column(String, default="")
    meta_description: Mapped[str] = mapped_column(String, default="")
    nav_label: Mapped[str] = mapped_column(String, default="")
    show_in_nav: Mapped[bool] = mapped_column(Boolean, default=False)
    nav_order: Mapped[int] = mapped_column(Integer, default=0)
    is_system: Mapped[bool] = mapped_column(Boolean, default=False)
    is_published: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    blocks: Mapped[list["ContentBlock"]] = relationship(
        back_populates="page", cascade="all, delete-orphan", order_by="ContentBlock.position"
    )


class ContentBlock(Base):
    """One editable piece of content on a Page. block_type determines which
    keys are meaningful inside data_json (see services.BLOCK_FIELDS).
    Ordering is via `position`; consecutive blocks of type feature_item or
    faq_item are grouped visually by the renderer (see
    services.group_blocks_for_render)."""
    __tablename__ = "content_blocks"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=gen_id)
    page_id: Mapped[str] = mapped_column(ForeignKey("pages.id"), index=True)
    block_type: Mapped[str] = mapped_column(String)
    position: Mapped[int] = mapped_column(Integer, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    data_json: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    page: Mapped["Page"] = relationship(back_populates="blocks")


class Notification(Base):
    __tablename__ = "notifications"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=gen_id)
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True)
    user_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    type: Mapped[str] = mapped_column(String, default="general")
    title: Mapped[str] = mapped_column(String, default="")
    message: Mapped[str] = mapped_column(Text, default="")
    is_read: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class Subscription(Base):
    __tablename__ = "subscriptions"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=gen_id)
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), unique=True)
    provider: Mapped[str] = mapped_column(String, default="mock")
    external_customer_id: Mapped[str] = mapped_column(String, default="")
    external_subscription_id: Mapped[str] = mapped_column(String, default="")
    plan: Mapped[str] = mapped_column(String, default="free")
    status: Mapped[str] = mapped_column(String, default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
