import os
import uuid
from datetime import datetime, date
from pathlib import Path

from fastapi import FastAPI, Request, Depends, UploadFile, File, Form, HTTPException
from fastapi.responses import RedirectResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import select, func
from sqlalchemy.orm import Session

from app.db import Base, engine, get_db, session_scope
from app.models import (
    User, Organization, Vendor, Invoice, ValidationResult, ValidationRule,
    Notification, SiteSettings, Page, ContentBlock, Subscription, PLAN_LIMITS, ROLES,
)
from app.security import (
    hash_password, verify_password, create_session_token, read_session_token,
    SESSION_COOKIE,
)
from app.services import (
    ValidationEngine, get_ai_provider, get_billing_provider, file_hash,
    invoices_used_this_month, plan_limit_reached, users_used, user_limit_reached,
    log_audit, risk_level_for, ensure_default_rules, notify_reviewers,
    STRIPE_ENABLED, StripeBillingProvider, apply_stripe_event,
    ensure_site_settings, site_settings_dict, DEFAULT_PRODUCT_NAME,
    ensure_default_pages, is_slug_available, add_block, move_block,
    group_blocks_for_render, BLOCK_TYPES, BLOCK_FIELDS, BLOCK_TYPE_LABELS,
    RESERVED_SLUGS,
)

BASE_DIR = Path(__file__).resolve().parent.parent
STORAGE_PATH = Path(os.environ.get("STORAGE_PATH", BASE_DIR / "uploads"))
STORAGE_PATH.mkdir(parents=True, exist_ok=True)
BRANDING_DIR = STORAGE_PATH / "branding"
BRANDING_DIR.mkdir(parents=True, exist_ok=True)
CONTENT_DIR = STORAGE_PATH / "content"
CONTENT_DIR.mkdir(parents=True, exist_ok=True)
ALLOWED_EXTENSIONS = {".pdf", ".png", ".jpg", ".jpeg"}
ALLOWED_LOGO_EXTENSIONS = {".png", ".jpg", ".jpeg", ".svg"}
MAX_FILE_SIZE = 10 * 1024 * 1024  # 10 MB
MAX_LOGO_SIZE = 2 * 1024 * 1024  # 2 MB
MAX_CONTENT_IMAGE_SIZE = 5 * 1024 * 1024  # 5 MB

Base.metadata.create_all(bind=engine)
with session_scope() as _startup_db:
    ensure_default_pages(_startup_db)

app = FastAPI(title=os.environ.get("APP_NAME", DEFAULT_PRODUCT_NAME))
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
app.mount("/branding-assets", StaticFiles(directory=str(BRANDING_DIR)), name="branding-assets")
app.mount("/content-assets", StaticFiles(directory=str(CONTENT_DIR)), name="content-assets")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
templates.env.globals["PLAN_LIMITS"] = PLAN_LIMITS
templates.env.globals["ROLES"] = ROLES
templates.env.globals["STRIPE_ENABLED"] = STRIPE_ENABLED
templates.env.globals["BLOCK_TYPES"] = BLOCK_TYPES
templates.env.globals["BLOCK_FIELDS"] = BLOCK_FIELDS
templates.env.globals["BLOCK_TYPE_LABELS"] = BLOCK_TYPE_LABELS


@app.middleware("http")
async def inject_site_branding(request: Request, call_next):
    """Loads the (singleton) branding row and the published nav pages once
    per request and stashes plain data on request.state so every template
    - including the standalone landing/login/register pages - can render
    {{ site.product_name }} / {{ nav_pages }} without every route handler
    needing to fetch them individually."""
    with session_scope() as db:
        settings = ensure_site_settings(db)
        request.state.site = site_settings_dict(settings)
        nav_pages = db.scalars(
            select(Page).where(Page.is_published.is_(True), Page.show_in_nav.is_(True))
            .order_by(Page.nav_order, Page.title)
        ).all()
        request.state.nav_pages = [{"slug": p.slug, "label": p.nav_label or p.title} for p in nav_pages]
    return await call_next(request)


# ------------------------------------------------------------- auth dep ----

def get_current_user(request: Request, db: Session) -> User | None:
    token = request.cookies.get(SESSION_COOKIE)
    user_id = read_session_token(token) if token else None
    if not user_id:
        return None
    return db.get(User, user_id)


def require_user(request: Request, db: Session = Depends(get_db)) -> User:
    user = get_current_user(request, db)
    if not user or not user.is_active:
        raise HTTPException(status_code=303, headers={"Location": "/login"})
    return user


def require_admin(request: Request, db: Session = Depends(get_db)) -> User:
    user = require_user(request, db)
    if user.role not in ("owner", "admin") and not user.is_platform_admin:
        raise HTTPException(status_code=403, detail="Not authorized")
    return user


def require_platform_admin(request: Request, db: Session = Depends(get_db)) -> User:
    user = require_user(request, db)
    if not user.is_platform_admin:
        raise HTTPException(status_code=404)
    return user


@app.exception_handler(HTTPException)
async def auth_redirect_handler(request: Request, exc: HTTPException):
    if exc.status_code == 303 and exc.headers and exc.headers.get("Location"):
        return RedirectResponse(url=exc.headers["Location"], status_code=303)
    return templates.TemplateResponse(
        request, "error.html", {"code": exc.status_code, "detail": exc.detail}, status_code=exc.status_code
    )


def tpl(request: Request, name: str, ctx: dict, status_code: int = 200):
    ctx["request"] = request
    ctx.setdefault("user", None)
    ctx.setdefault("site", getattr(request.state, "site", {
        "product_name": DEFAULT_PRODUCT_NAME, "tagline": "", "logo_url": "", "support_email": "",
        "term_record_singular": "Invoice", "term_record_plural": "Invoices",
        "term_party_singular": "Vendor", "term_party_plural": "Vendors",
    }))
    ctx.setdefault("nav_pages", getattr(request.state, "nav_pages", []))
    return templates.TemplateResponse(name, ctx, status_code=status_code)


def parse_date(s: str | None):
    try:
        return date.fromisoformat(s) if s else None
    except ValueError:
        return None


def render_public_page(request: Request, db: Session, page: Page, status_code: int = 200):
    active_blocks = [b for b in page.blocks if b.is_active]
    groups = group_blocks_for_render(active_blocks)
    return tpl(request, "cms_page.html", {
        "page": page, "groups": groups,
    }, status_code=status_code)


# ------------------------------------------------------------- landing -----

@app.get("/", response_class=HTMLResponse)
def landing(request: Request, db: Session = Depends(get_db)):
    page = db.scalar(select(Page).where(Page.slug == "home"))
    if not page:
        raise HTTPException(500, "Home page is not configured.")
    return render_public_page(request, db, page)


@app.get("/pricing", response_class=HTMLResponse)
def pricing(request: Request, db: Session = Depends(get_db)):
    page = db.scalar(select(Page).where(Page.slug == "pricing"))
    if not page:
        raise HTTPException(500, "Pricing page is not configured.")
    return render_public_page(request, db, page)


# ------------------------------------------------------------- auth --------

@app.get("/register", response_class=HTMLResponse)
def register_form(request: Request):
    return tpl(request, "register.html", {"error": None})


@app.post("/register")
def register(
    request: Request,
    company_name: str = Form(...),
    email: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    email = email.strip().lower()
    existing = db.scalar(select(User).where(User.email == email))
    if existing:
        return tpl(request, "register.html", {"error": "An account with that email already exists."}, 400)
    if len(password) < 8:
        return tpl(request, "register.html", {"error": "Password must be at least 8 characters."}, 400)

    slug_base = "".join(c for c in company_name.lower().replace(" ", "-") if c.isalnum() or c == "-") or "org"
    slug = slug_base
    n = 1
    while db.scalar(select(Organization).where(Organization.slug == slug)):
        n += 1
        slug = f"{slug_base}-{n}"

    org = Organization(name=company_name, slug=slug, email=email, plan="free")
    db.add(org)
    db.flush()

    user = User(
        organization_id=org.id, email=email, password_hash=hash_password(password),
        first_name=company_name.split(" ")[0], role="owner",
    )
    db.add(user)
    db.add(Subscription(organization_id=org.id, plan="free", status="active"))
    ensure_default_rules(db, org.id)
    log_audit(db, org.id, user.id, "organization.created", "organization", org.id)
    log_audit(db, org.id, user.id, "user.created", "user", user.id)
    db.commit()

    resp = RedirectResponse(url="/dashboard", status_code=303)
    resp.set_cookie(SESSION_COOKIE, create_session_token(user.id), httponly=True, samesite="lax", max_age=1209600)
    return resp


@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request):
    return tpl(request, "login.html", {"error": None})


@app.post("/login")
def login(request: Request, email: str = Form(...), password: str = Form(...), db: Session = Depends(get_db)):
    email = email.strip().lower()
    user = db.scalar(select(User).where(User.email == email))
    if not user or not verify_password(password, user.password_hash) or not user.is_active:
        return tpl(request, "login.html", {"error": "Invalid email or password."}, 400)
    user.last_login_at = datetime.utcnow()
    db.commit()
    resp = RedirectResponse(url="/dashboard", status_code=303)
    resp.set_cookie(SESSION_COOKIE, create_session_token(user.id), httponly=True, samesite="lax", max_age=1209600)
    return resp


@app.get("/logout")
def logout():
    resp = RedirectResponse(url="/", status_code=303)
    resp.delete_cookie(SESSION_COOKIE)
    return resp


# ------------------------------------------------------------- dashboard ---

@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    org_id = user.organization_id
    start_of_month = datetime.utcnow().replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    invoices_this_month = db.scalar(
        select(func.count(Invoice.id)).where(Invoice.organization_id == org_id, Invoice.created_at >= start_of_month)
    ) or 0
    spend_this_month = db.scalar(
        select(func.coalesce(func.sum(Invoice.total), 0)).where(
            Invoice.organization_id == org_id, Invoice.created_at >= start_of_month
        )
    ) or 0
    needs_review = db.scalar(
        select(func.count(Invoice.id)).where(Invoice.organization_id == org_id, Invoice.status == "needs_review")
    ) or 0
    high_risk = db.scalar(
        select(func.count(Invoice.id)).where(
            Invoice.organization_id == org_id, Invoice.risk_level.in_(["high", "critical"])
        )
    ) or 0
    potential_savings = db.scalar(
        select(func.coalesce(func.sum(Invoice.total), 0)).where(
            Invoice.organization_id == org_id,
            ((Invoice.risk_level.in_(["high", "critical"])) | (Invoice.status == "rejected")),
        )
    ) or 0

    recent_invoices = db.scalars(
        select(Invoice).where(Invoice.organization_id == org_id).order_by(Invoice.created_at.desc()).limit(8)
    ).all()

    org = db.get(Organization, org_id)
    used = invoices_used_this_month(db, org_id)
    limit = org.limits()["invoices"]
    unread_notifications = db.scalar(
        select(func.count(Notification.id)).where(
            Notification.organization_id == org_id, Notification.user_id == user.id, Notification.is_read.is_(False)
        )
    ) or 0

    return tpl(request, "dashboard.html", {
        "user": user,
        "org": org,
        "invoices_this_month": invoices_this_month,
        "spend_this_month": spend_this_month,
        "needs_review": needs_review,
        "high_risk": high_risk,
        "potential_savings": potential_savings,
        "recent_invoices": recent_invoices,
        "used": used,
        "limit": limit,
        "pct_used": min(100, round(used / limit * 100)) if limit else 0,
        "unread_notifications": unread_notifications,
    })


# ------------------------------------------------------------- invoices ----

@app.get("/invoices", response_class=HTMLResponse)
def invoices_list(request: Request, status: str = "", q: str = "",
                   user: User = Depends(require_user), db: Session = Depends(get_db)):
    stmt = select(Invoice).where(Invoice.organization_id == user.organization_id)
    if status:
        stmt = stmt.where(Invoice.status == status)
    if q:
        stmt = stmt.where(Invoice.invoice_number.ilike(f"%{q}%"))
    invoices = db.scalars(stmt.order_by(Invoice.created_at.desc())).all()
    return tpl(request, "invoices.html", {"invoices": invoices, "status": status, "q": q, "user": user})


@app.get("/invoices/upload", response_class=HTMLResponse)
def upload_form(request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    org = db.get(Organization, user.organization_id)
    vendors = db.scalars(
        select(Vendor).where(Vendor.organization_id == user.organization_id).order_by(Vendor.name)
    ).all()
    limit_reached = plan_limit_reached(db, org)
    return tpl(request, "upload.html", {"vendors": vendors, "limit_reached": limit_reached, "org": org, "user": user})


@app.post("/invoices/upload")
async def upload_invoice(
    request: Request,
    vendor_id: str = Form(""),
    invoice_number: str = Form(""),
    invoice_date: str = Form(""),
    due_date: str = Form(""),
    po_number: str = Form(""),
    subtotal: float = Form(0),
    tax: float = Form(0),
    total: float = Form(0),
    file: UploadFile | None = File(None),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    org = db.get(Organization, user.organization_id)
    if plan_limit_reached(db, org):
        return RedirectResponse(url="/billing?limit_reached=1", status_code=303)

    document_path = ""
    doc_hash = ""
    if file and file.filename:
        ext = Path(file.filename).suffix.lower()
        if ext not in ALLOWED_EXTENSIONS:
            raise HTTPException(400, "Unsupported file type. Allowed: PDF, PNG, JPG.")
        content = await file.read()
        if len(content) > MAX_FILE_SIZE:
            raise HTTPException(400, "File exceeds the 10 MB limit.")
        doc_hash = file_hash(content)
        safe_name = f"{uuid.uuid4().hex}{ext}"
        org_dir = STORAGE_PATH / org.id
        org_dir.mkdir(parents=True, exist_ok=True)
        (org_dir / safe_name).write_bytes(content)
        document_path = str(Path(org.id) / safe_name)

    invoice = Invoice(
        organization_id=org.id,
        vendor_id=vendor_id or None,
        invoice_number=invoice_number.strip(),
        invoice_date=parse_date(invoice_date),
        due_date=parse_date(due_date),
        po_number=po_number.strip(),
        subtotal=subtotal, tax=tax, total=total or (subtotal + tax),
        document_path=document_path, document_hash=doc_hash,
        source="upload" if file else "manual",
        status="processed",
    )
    db.add(invoice)
    db.flush()

    engine_ = ValidationEngine(db, org.id)
    findings, score = engine_.run(invoice)
    for f in findings:
        f.invoice_id = invoice.id
        db.add(f)

    ai = get_ai_provider()
    analysis = ai.analyze_invoice(invoice, findings, score)
    invoice.risk_score = score
    invoice.risk_level = analysis["risk_level"]
    invoice.ai_summary = analysis["summary"]
    invoice.status = "needs_review" if score >= 30 else "approved"
    invoice.approval_status = "pending" if score >= 30 else "approved"

    if invoice.status == "needs_review":
        notify_reviewers(db, org.id, invoice)

    log_audit(db, org.id, user.id, "invoice.created", "invoice", invoice.id, f"risk_score={score}")
    db.commit()
    return RedirectResponse(url=f"/invoices/{invoice.id}", status_code=303)


@app.get("/invoices/{invoice_id}", response_class=HTMLResponse)
def invoice_detail(request: Request, invoice_id: str, user: User = Depends(require_user), db: Session = Depends(get_db)):
    invoice = db.get(Invoice, invoice_id)
    if not invoice or invoice.organization_id != user.organization_id:
        raise HTTPException(404, "Invoice not found")
    findings = db.scalars(
        select(ValidationResult).where(ValidationResult.invoice_id == invoice.id)
    ).all()
    return tpl(request, "invoice_detail.html", {"invoice": invoice, "findings": findings, "user": user})


@app.get("/invoices/{invoice_id}/edit", response_class=HTMLResponse)
def edit_invoice_form(request: Request, invoice_id: str, user: User = Depends(require_user), db: Session = Depends(get_db)):
    invoice = db.get(Invoice, invoice_id)
    if not invoice or invoice.organization_id != user.organization_id:
        raise HTTPException(404, "Invoice not found")
    vendors = db.scalars(
        select(Vendor).where(Vendor.organization_id == user.organization_id).order_by(Vendor.name)
    ).all()
    return tpl(request, "invoice_edit.html", {"invoice": invoice, "vendors": vendors, "user": user})


@app.post("/invoices/{invoice_id}")
def update_invoice(
    invoice_id: str,
    vendor_id: str = Form(""),
    invoice_number: str = Form(""),
    invoice_date: str = Form(""),
    due_date: str = Form(""),
    po_number: str = Form(""),
    subtotal: float = Form(0),
    tax: float = Form(0),
    total: float = Form(0),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    invoice = db.get(Invoice, invoice_id)
    if not invoice or invoice.organization_id != user.organization_id:
        raise HTTPException(404, "Invoice not found")

    invoice.vendor_id = vendor_id or None
    invoice.invoice_number = invoice_number.strip()
    invoice.invoice_date = parse_date(invoice_date)
    invoice.due_date = parse_date(due_date)
    invoice.po_number = po_number.strip()
    invoice.subtotal = subtotal
    invoice.tax = tax
    invoice.total = total or (subtotal + tax)

    # Re-run validation since the underlying facts changed.
    db.query(ValidationResult).filter(ValidationResult.invoice_id == invoice.id).delete()
    engine_ = ValidationEngine(db, user.organization_id)
    findings, score = engine_.run(invoice)
    for f in findings:
        f.invoice_id = invoice.id
        db.add(f)
    ai = get_ai_provider()
    analysis = ai.analyze_invoice(invoice, findings, score)
    invoice.risk_score = score
    invoice.risk_level = analysis["risk_level"]
    invoice.ai_summary = analysis["summary"]
    if invoice.approval_status == "pending":
        invoice.status = "needs_review" if score >= 30 else "approved"
        if score < 30:
            invoice.approval_status = "approved"

    log_audit(db, user.organization_id, user.id, "invoice.updated", "invoice", invoice.id)
    db.commit()
    return RedirectResponse(url=f"/invoices/{invoice.id}", status_code=303)


@app.post("/invoices/{invoice_id}/approve")
def approve_invoice(invoice_id: str, user: User = Depends(require_user), db: Session = Depends(get_db)):
    invoice = db.get(Invoice, invoice_id)
    if not invoice or invoice.organization_id != user.organization_id:
        raise HTTPException(404)
    invoice.status = "approved"
    invoice.approval_status = "approved"
    log_audit(db, user.organization_id, user.id, "invoice.approved", "invoice", invoice.id)
    db.commit()
    return RedirectResponse(url=f"/invoices/{invoice_id}", status_code=303)


@app.post("/invoices/{invoice_id}/reject")
def reject_invoice(invoice_id: str, user: User = Depends(require_user), db: Session = Depends(get_db)):
    invoice = db.get(Invoice, invoice_id)
    if not invoice or invoice.organization_id != user.organization_id:
        raise HTTPException(404)
    invoice.status = "rejected"
    invoice.approval_status = "rejected"
    log_audit(db, user.organization_id, user.id, "invoice.rejected", "invoice", invoice.id)
    db.commit()
    return RedirectResponse(url=f"/invoices/{invoice_id}", status_code=303)


@app.post("/invoices/{invoice_id}/delete")
def delete_invoice(invoice_id: str, user: User = Depends(require_admin), db: Session = Depends(get_db)):
    invoice = db.get(Invoice, invoice_id)
    if not invoice or invoice.organization_id != user.organization_id:
        raise HTTPException(404)
    db.delete(invoice)
    log_audit(db, user.organization_id, user.id, "invoice.deleted", "invoice", invoice_id)
    db.commit()
    return RedirectResponse(url="/invoices", status_code=303)


# ------------------------------------------------------------- vendors -----

@app.get("/vendors", response_class=HTMLResponse)
def vendors_list(request: Request, q: str = "", user: User = Depends(require_user), db: Session = Depends(get_db)):
    stmt = select(Vendor).where(Vendor.organization_id == user.organization_id)
    if q:
        stmt = stmt.where(Vendor.name.ilike(f"%{q}%"))
    vendors = db.scalars(stmt.order_by(Vendor.name)).all()

    spend_by_vendor = {}
    rows = db.execute(
        select(Invoice.vendor_id, func.sum(Invoice.total), func.count(Invoice.id))
        .where(Invoice.organization_id == user.organization_id)
        .group_by(Invoice.vendor_id)
    ).all()
    for vid, total, count in rows:
        spend_by_vendor[vid] = {"total": total or 0, "count": count or 0}

    return tpl(request, "vendors.html", {"vendors": vendors, "q": q, "spend": spend_by_vendor, "user": user})


@app.post("/vendors")
def create_vendor(
    request: Request, name: str = Form(...), email: str = Form(""), phone: str = Form(""),
    address: str = Form(""), tax_id: str = Form(""), payment_terms: str = Form("Net 30"),
    user: User = Depends(require_user), db: Session = Depends(get_db),
):
    vendor = Vendor(organization_id=user.organization_id, name=name.strip(), email=email,
                     phone=phone, address=address, tax_id=tax_id, payment_terms=payment_terms)
    db.add(vendor)
    db.flush()
    log_audit(db, user.organization_id, user.id, "vendor.created", "vendor", vendor.id)
    db.commit()
    return RedirectResponse(url="/vendors", status_code=303)


@app.get("/vendors/{vendor_id}/edit", response_class=HTMLResponse)
def edit_vendor_form(request: Request, vendor_id: str, user: User = Depends(require_user), db: Session = Depends(get_db)):
    vendor = db.get(Vendor, vendor_id)
    if not vendor or vendor.organization_id != user.organization_id:
        raise HTTPException(404)
    return tpl(request, "vendor_edit.html", {"vendor": vendor, "user": user})


@app.post("/vendors/{vendor_id}")
def update_vendor(
    vendor_id: str, name: str = Form(...), email: str = Form(""), phone: str = Form(""),
    address: str = Form(""), tax_id: str = Form(""), payment_terms: str = Form("Net 30"),
    status: str = Form("active"), user: User = Depends(require_user), db: Session = Depends(get_db),
):
    vendor = db.get(Vendor, vendor_id)
    if not vendor or vendor.organization_id != user.organization_id:
        raise HTTPException(404)
    vendor.name = name.strip()
    vendor.email = email
    vendor.phone = phone
    vendor.address = address
    vendor.tax_id = tax_id
    vendor.payment_terms = payment_terms
    vendor.status = status if status in ("active", "inactive") else vendor.status
    log_audit(db, user.organization_id, user.id, "vendor.updated", "vendor", vendor.id)
    db.commit()
    return RedirectResponse(url="/vendors", status_code=303)


@app.post("/vendors/{vendor_id}/delete")
def delete_vendor(vendor_id: str, user: User = Depends(require_admin), db: Session = Depends(get_db)):
    vendor = db.get(Vendor, vendor_id)
    if not vendor or vendor.organization_id != user.organization_id:
        raise HTTPException(404)
    db.delete(vendor)
    log_audit(db, user.organization_id, user.id, "vendor.deleted", "vendor", vendor_id)
    db.commit()
    return RedirectResponse(url="/vendors", status_code=303)


# ------------------------------------------------------------- billing -----

@app.get("/billing", response_class=HTMLResponse)
def billing_page(
    request: Request, limit_reached: int = 0, upgraded: int = 0,
    user: User = Depends(require_user), db: Session = Depends(get_db),
):
    org = db.get(Organization, user.organization_id)
    used = invoices_used_this_month(db, org.id)
    return tpl(request, "billing.html", {
        "org": org, "used": used, "limit_reached": bool(limit_reached),
        "upgraded": bool(upgraded), "user": user,
    })


@app.post("/billing/checkout")
def start_checkout(
    request: Request, plan: str = Form(...),
    user: User = Depends(require_admin), db: Session = Depends(get_db),
):
    """Real payment flow: creates a Stripe Checkout Session (a hosted page
    where the user enters their card and pays) and redirects there."""
    if not STRIPE_ENABLED:
        raise HTTPException(400, "Stripe is not configured. Set STRIPE_SECRET_KEY to enable real payments.")
    if plan not in ("starter", "professional", "business"):
        raise HTTPException(400, "Unknown plan")
    org = db.get(Organization, user.organization_id)
    billing: StripeBillingProvider = get_billing_provider()
    base_url = str(request.base_url).rstrip("/")
    try:
        checkout_url = billing.create_checkout_session(
            db, org, plan,
            success_url=f"{base_url}/billing?upgraded=1",
            cancel_url=f"{base_url}/billing",
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(502, f"Stripe request failed: {e}")
    db.commit()
    return RedirectResponse(url=checkout_url, status_code=303)


@app.post("/billing/portal")
def open_billing_portal(
    request: Request, user: User = Depends(require_admin), db: Session = Depends(get_db),
):
    """Sends the user to the Stripe Customer Portal to change plans, update
    their card, view invoices, or cancel - Stripe hosts all of this so we
    never touch card data."""
    if not STRIPE_ENABLED:
        raise HTTPException(400, "Stripe is not configured.")
    org = db.get(Organization, user.organization_id)
    billing: StripeBillingProvider = get_billing_provider()
    base_url = str(request.base_url).rstrip("/")
    try:
        portal_url = billing.create_portal_session(db, org, return_url=f"{base_url}/billing")
    except Exception as e:
        raise HTTPException(502, f"Stripe request failed: {e}")
    db.commit()
    return RedirectResponse(url=portal_url, status_code=303)


@app.post("/billing/webhook")
async def stripe_webhook(request: Request, db: Session = Depends(get_db)):
    """Receives events from Stripe (subscription created/updated/canceled,
    payment failed, checkout completed) and syncs local plan/status state.
    Configure this URL in the Stripe Dashboard as
    https://yourdomain.com/billing/webhook and put the signing secret in
    STRIPE_WEBHOOK_SECRET."""
    if not STRIPE_ENABLED:
        raise HTTPException(400, "Stripe is not configured.")
    import stripe
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature", "")
    webhook_secret = os.environ.get("STRIPE_WEBHOOK_SECRET")
    try:
        if webhook_secret:
            event = stripe.Webhook.construct_event(payload, sig_header, webhook_secret)
        else:
            # No signing secret configured (e.g. quick local testing with the
            # Stripe CLI) - parse without verification. Not recommended for
            # production; set STRIPE_WEBHOOK_SECRET before going live.
            import json
            event = json.loads(payload)
    except (ValueError, Exception) as e:
        raise HTTPException(400, f"Invalid webhook payload: {e}")

    event_type = event["type"] if isinstance(event, dict) else event.type
    data_object = event["data"]["object"] if isinstance(event, dict) else event.data.object

    apply_stripe_event(db, event_type, data_object)
    db.commit()
    return {"received": True}


@app.post("/billing/upgrade")
def upgrade_plan_mock(plan: str = Form(...), user: User = Depends(require_admin), db: Session = Depends(get_db)):
    """Development-only plan switch used when Stripe isn't configured, so
    the app is still fully usable without a Stripe account. Once
    STRIPE_SECRET_KEY is set, upgrades go through /billing/checkout instead
    and this route refuses to run."""
    if STRIPE_ENABLED:
        raise HTTPException(400, "Stripe is configured - use checkout/portal instead of the mock upgrade.")
    if plan not in PLAN_LIMITS:
        raise HTTPException(400, "Unknown plan")
    org = db.get(Organization, user.organization_id)
    billing = get_billing_provider()
    result = billing.change_plan(org, plan)
    org.plan = plan
    org.subscription_status = result["status"]
    sub = db.scalar(select(Subscription).where(Subscription.organization_id == org.id))
    if sub:
        sub.plan = plan
        sub.status = result["status"]
    else:
        db.add(Subscription(organization_id=org.id, provider="mock", plan=plan, status=result["status"]))
    log_audit(db, org.id, user.id, "subscription.changed", "organization", org.id, f"plan={plan}")
    db.commit()
    return RedirectResponse(url="/billing", status_code=303)


# ------------------------------------------------------------- settings ----

@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, user: User = Depends(require_admin), db: Session = Depends(get_db)):
    org = db.get(Organization, user.organization_id)
    return tpl(request, "settings/organization.html", {"org": org, "user": user})


@app.post("/settings")
def update_settings(
    name: str = Form(...), email: str = Form(...),
    user: User = Depends(require_admin), db: Session = Depends(get_db),
):
    org = db.get(Organization, user.organization_id)
    org.name = name.strip()
    org.email = email.strip().lower()
    log_audit(db, org.id, user.id, "organization.updated", "organization", org.id)
    db.commit()
    return RedirectResponse(url="/settings", status_code=303)


# --------------------------------------------------------- settings/users --

@app.get("/settings/users", response_class=HTMLResponse)
def users_list_page(request: Request, error: str = "", user: User = Depends(require_admin), db: Session = Depends(get_db)):
    org = db.get(Organization, user.organization_id)
    org_users = db.scalars(
        select(User).where(User.organization_id == user.organization_id).order_by(User.created_at)
    ).all()
    return tpl(request, "settings/users.html", {
        "org": org, "users": org_users, "user": user, "error": error,
        "user_count": users_used(db, org.id), "user_limit": org.limits()["users"],
    })


@app.post("/settings/users")
def create_user(
    email: str = Form(...), password: str = Form(...), first_name: str = Form(""),
    last_name: str = Form(""), role: str = Form("viewer"),
    user: User = Depends(require_admin), db: Session = Depends(get_db),
):
    org = db.get(Organization, user.organization_id)
    if user_limit_reached(db, org):
        return RedirectResponse(url="/settings/users?error=limit", status_code=303)
    email = email.strip().lower()
    if db.scalar(select(User).where(User.email == email)):
        return RedirectResponse(url="/settings/users?error=exists", status_code=303)
    if len(password) < 8:
        return RedirectResponse(url="/settings/users?error=password", status_code=303)
    if role not in ROLES:
        role = "viewer"

    new_user = User(
        organization_id=org.id, email=email, password_hash=hash_password(password),
        first_name=first_name.strip(), last_name=last_name.strip(), role=role,
    )
    db.add(new_user)
    db.flush()
    log_audit(db, org.id, user.id, "user.created", "user", new_user.id)
    db.commit()
    return RedirectResponse(url="/settings/users", status_code=303)


@app.get("/settings/users/{user_id}/edit", response_class=HTMLResponse)
def edit_user_form(request: Request, user_id: str, user: User = Depends(require_admin), db: Session = Depends(get_db)):
    target = db.get(User, user_id)
    if not target or target.organization_id != user.organization_id:
        raise HTTPException(404)
    return tpl(request, "settings/user_edit.html", {"target": target, "user": user})


@app.post("/settings/users/{user_id}")
def update_user(
    user_id: str, first_name: str = Form(""), last_name: str = Form(""), role: str = Form(...),
    user: User = Depends(require_admin), db: Session = Depends(get_db),
):
    target = db.get(User, user_id)
    if not target or target.organization_id != user.organization_id:
        raise HTTPException(404)
    if role not in ROLES:
        raise HTTPException(400, "Invalid role")
    target.first_name = first_name.strip()
    target.last_name = last_name.strip()
    # Prevent locking yourself out by changing your own role.
    if target.id != user.id and role != target.role:
        old_role = target.role
        target.role = role
        log_audit(db, user.organization_id, user.id, "user.role_changed", "user", target.id, f"{old_role} -> {role}")
    log_audit(db, user.organization_id, user.id, "user.updated", "user", target.id)
    db.commit()
    return RedirectResponse(url="/settings/users", status_code=303)


@app.post("/settings/users/{user_id}/toggle-active")
def toggle_user_active(user_id: str, user: User = Depends(require_admin), db: Session = Depends(get_db)):
    target = db.get(User, user_id)
    if not target or target.organization_id != user.organization_id:
        raise HTTPException(404)
    if target.id == user.id:
        raise HTTPException(400, "You cannot deactivate your own account.")
    target.is_active = not target.is_active
    log_audit(db, user.organization_id, user.id,
              "user.activated" if target.is_active else "user.deactivated", "user", target.id)
    db.commit()
    return RedirectResponse(url="/settings/users", status_code=303)


@app.post("/settings/users/{user_id}/delete")
def delete_user(user_id: str, user: User = Depends(require_admin), db: Session = Depends(get_db)):
    target = db.get(User, user_id)
    if not target or target.organization_id != user.organization_id:
        raise HTTPException(404)
    if target.id == user.id:
        raise HTTPException(400, "You cannot delete your own account.")
    if target.role == "owner":
        owners = db.scalar(
            select(func.count(User.id)).where(User.organization_id == user.organization_id, User.role == "owner")
        )
        if owners <= 1:
            raise HTTPException(400, "Organization must have at least one owner.")
    db.delete(target)
    log_audit(db, user.organization_id, user.id, "user.deleted", "user", user_id)
    db.commit()
    return RedirectResponse(url="/settings/users", status_code=303)


# -------------------------------------------------------- settings/rules ---

@app.get("/settings/rules", response_class=HTMLResponse)
def rules_list_page(request: Request, user: User = Depends(require_admin), db: Session = Depends(get_db)):
    ensure_default_rules(db, user.organization_id)
    db.commit()
    rules = db.scalars(
        select(ValidationRule).where(ValidationRule.organization_id == user.organization_id)
        .order_by(ValidationRule.created_at)
    ).all()
    return tpl(request, "settings/rules.html", {"rules": rules, "user": user})


@app.post("/settings/rules")
def create_rule(
    threshold: float = Form(...), severity: str = Form("high"),
    user: User = Depends(require_admin), db: Session = Depends(get_db),
):
    if severity not in ("low", "medium", "high"):
        severity = "medium"
    rule = ValidationRule(
        organization_id=user.organization_id,
        name=f"Amount at or above ${threshold:,.0f}",
        description="Custom rule: flags any invoice at or above this total.",
        rule_type="amount_threshold",
        configuration_json={"threshold": threshold},
        severity=severity, is_active=True,
    )
    db.add(rule)
    db.flush()
    log_audit(db, user.organization_id, user.id, "validation_rule.created", "validation_rule", rule.id)
    db.commit()
    return RedirectResponse(url="/settings/rules", status_code=303)


@app.post("/settings/rules/{rule_id}")
def update_rule(
    rule_id: str, severity: str = Form(...), threshold: float | None = Form(None),
    user: User = Depends(require_admin), db: Session = Depends(get_db),
):
    rule = db.get(ValidationRule, rule_id)
    if not rule or rule.organization_id != user.organization_id:
        raise HTTPException(404)
    if severity not in ("low", "medium", "high"):
        raise HTTPException(400, "Invalid severity")
    rule.severity = severity
    if rule.rule_type == "amount_threshold" and threshold is not None:
        rule.configuration_json = {"threshold": threshold}
        rule.name = f"Amount at or above ${threshold:,.0f}"
    log_audit(db, user.organization_id, user.id, "validation_rule.updated", "validation_rule", rule.id)
    db.commit()
    return RedirectResponse(url="/settings/rules", status_code=303)


@app.post("/settings/rules/{rule_id}/toggle")
def toggle_rule(rule_id: str, user: User = Depends(require_admin), db: Session = Depends(get_db)):
    rule = db.get(ValidationRule, rule_id)
    if not rule or rule.organization_id != user.organization_id:
        raise HTTPException(404)
    rule.is_active = not rule.is_active
    log_audit(db, user.organization_id, user.id, "validation_rule.toggled", "validation_rule", rule.id)
    db.commit()
    return RedirectResponse(url="/settings/rules", status_code=303)


@app.post("/settings/rules/{rule_id}/delete")
def delete_rule(rule_id: str, user: User = Depends(require_admin), db: Session = Depends(get_db)):
    rule = db.get(ValidationRule, rule_id)
    if not rule or rule.organization_id != user.organization_id:
        raise HTTPException(404)
    db.delete(rule)
    log_audit(db, user.organization_id, user.id, "validation_rule.deleted", "validation_rule", rule_id)
    db.commit()
    return RedirectResponse(url="/settings/rules", status_code=303)


# ------------------------------------------------------------ notifications

@app.get("/notifications", response_class=HTMLResponse)
def notifications_list(request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    notifs = db.scalars(
        select(Notification).where(
            Notification.organization_id == user.organization_id, Notification.user_id == user.id
        ).order_by(Notification.created_at.desc())
    ).all()
    return tpl(request, "notifications.html", {"notifications": notifs, "user": user})


@app.post("/notifications/{notif_id}/read")
def mark_notification_read(notif_id: str, user: User = Depends(require_user), db: Session = Depends(get_db)):
    n = db.get(Notification, notif_id)
    if not n or n.organization_id != user.organization_id or n.user_id != user.id:
        raise HTTPException(404)
    n.is_read = True
    db.commit()
    return RedirectResponse(url="/notifications", status_code=303)


@app.post("/notifications/mark-all-read")
def mark_all_notifications_read(user: User = Depends(require_user), db: Session = Depends(get_db)):
    db.query(Notification).filter(
        Notification.organization_id == user.organization_id, Notification.user_id == user.id,
    ).update({"is_read": True})
    db.commit()
    return RedirectResponse(url="/notifications", status_code=303)


@app.post("/notifications/{notif_id}/delete")
def delete_notification(notif_id: str, user: User = Depends(require_user), db: Session = Depends(get_db)):
    n = db.get(Notification, notif_id)
    if not n or n.organization_id != user.organization_id or n.user_id != user.id:
        raise HTTPException(404)
    db.delete(n)
    db.commit()
    return RedirectResponse(url="/notifications", status_code=303)


# ------------------------------------------------------------- admin -------

@app.get("/admin", response_class=HTMLResponse)
def admin_overview(request: Request, user: User = Depends(require_platform_admin), db: Session = Depends(get_db)):
    orgs = db.scalars(select(Organization).order_by(Organization.created_at.desc())).all()
    mrr = sum(PLAN_LIMITS.get(o.plan, PLAN_LIMITS["free"])["price"] for o in orgs if o.subscription_status == "active")
    paying = sum(1 for o in orgs if o.plan != "free")
    total_invoices = db.scalar(select(func.count(Invoice.id))) or 0
    return tpl(request, "admin.html", {
        "user": user,
        "orgs": orgs, "mrr": mrr, "arr": mrr * 12, "paying": paying,
        "total_orgs": len(orgs), "total_invoices": total_invoices,
        "arpu": round(mrr / paying, 2) if paying else 0,
    })


@app.post("/admin/organizations/{org_id}/suspend")
def suspend_org(org_id: str, user: User = Depends(require_platform_admin), db: Session = Depends(get_db)):
    org = db.get(Organization, org_id)
    if not org:
        raise HTTPException(404)
    org.is_suspended = not org.is_suspended
    log_audit(db, org.id, user.id, "organization.suspend_toggled", "organization", org.id)
    db.commit()
    return RedirectResponse(url="/admin", status_code=303)


# ----------------------------------------------------------- branding CRUD -

@app.get("/admin/branding", response_class=HTMLResponse)
def branding_page(request: Request, user: User = Depends(require_platform_admin), db: Session = Depends(get_db)):
    settings = ensure_site_settings(db)
    db.commit()
    return tpl(request, "admin/branding.html", {"user": user, "settings": settings})


@app.post("/admin/branding")
async def update_branding(
    request: Request,
    product_name: str = Form(...),
    tagline: str = Form(""),
    support_email: str = Form(""),
    term_record_singular: str = Form("Invoice"),
    term_record_plural: str = Form("Invoices"),
    term_party_singular: str = Form("Vendor"),
    term_party_plural: str = Form("Vendors"),
    logo: UploadFile | None = File(None),
    remove_logo: str = Form(""),
    user: User = Depends(require_platform_admin),
    db: Session = Depends(get_db),
):
    settings = ensure_site_settings(db)
    settings.product_name = product_name.strip() or DEFAULT_PRODUCT_NAME
    settings.tagline = tagline.strip()
    settings.support_email = support_email.strip()
    settings.term_record_singular = term_record_singular.strip() or "Invoice"
    settings.term_record_plural = term_record_plural.strip() or "Invoices"
    settings.term_party_singular = term_party_singular.strip() or "Vendor"
    settings.term_party_plural = term_party_plural.strip() or "Vendors"

    def clear_logo_file():
        if settings.logo_url:
            old_path = BRANDING_DIR / Path(settings.logo_url).name
            old_path.unlink(missing_ok=True)
        settings.logo_url = ""

    if remove_logo == "1":
        clear_logo_file()

    if logo and logo.filename:
        ext = Path(logo.filename).suffix.lower()
        if ext not in ALLOWED_LOGO_EXTENSIONS:
            raise HTTPException(400, "Unsupported logo file type. Allowed: PNG, JPG, SVG.")
        content = await logo.read()
        if len(content) > MAX_LOGO_SIZE:
            raise HTTPException(400, "Logo file exceeds the 2 MB limit.")
        clear_logo_file()  # remove any previous logo file before writing the new one
        safe_name = f"logo-{uuid.uuid4().hex}{ext}"
        (BRANDING_DIR / safe_name).write_bytes(content)
        settings.logo_url = f"/branding-assets/{safe_name}"

    log_audit(db, user.organization_id, user.id, "site_settings.updated", "site_settings", "site")
    db.commit()
    return RedirectResponse(url="/admin/branding", status_code=303)


@app.post("/admin/branding/reset")
def reset_branding(user: User = Depends(require_platform_admin), db: Session = Depends(get_db)):
    settings = ensure_site_settings(db)
    if settings.logo_url:
        old_path = BRANDING_DIR / Path(settings.logo_url).name
        old_path.unlink(missing_ok=True)
    settings.product_name = DEFAULT_PRODUCT_NAME
    settings.tagline = os.environ.get("APP_TAGLINE", "Catch invoice problems before they cost you money.")
    settings.support_email = os.environ.get("APP_SUPPORT_EMAIL", "")
    settings.logo_url = ""
    settings.term_record_singular = "Invoice"
    settings.term_record_plural = "Invoices"
    settings.term_party_singular = "Vendor"
    settings.term_party_plural = "Vendors"
    log_audit(db, user.organization_id, user.id, "site_settings.reset", "site_settings", "site")
    db.commit()
    return RedirectResponse(url="/admin/branding", status_code=303)


# ----------------------------------------------------------------- pages ---

@app.get("/admin/pages", response_class=HTMLResponse)
def pages_list(request: Request, error: str = "", user: User = Depends(require_platform_admin), db: Session = Depends(get_db)):
    pages = db.scalars(select(Page).order_by(Page.is_system.desc(), Page.title)).all()
    return tpl(request, "admin/pages.html", {"pages": pages, "user": user, "error": error})


@app.post("/admin/pages")
def create_page(
    title: str = Form(...), slug: str = Form(...), nav_label: str = Form(""),
    show_in_nav: str = Form(""), user: User = Depends(require_platform_admin), db: Session = Depends(get_db),
):
    slug = slug.strip().lower().replace(" ", "-")
    if not is_slug_available(db, slug):
        return RedirectResponse(url="/admin/pages?error=slug", status_code=303)
    page = Page(
        title=title.strip(), slug=slug, nav_label=nav_label.strip(),
        show_in_nav=bool(show_in_nav), is_system=False, is_published=False,
    )
    db.add(page)
    db.flush()
    log_audit(db, user.organization_id, user.id, "page.created", "page", page.id)
    db.commit()
    return RedirectResponse(url=f"/admin/pages/{page.id}/edit", status_code=303)


@app.get("/admin/pages/{page_id}/edit", response_class=HTMLResponse)
def edit_page(request: Request, page_id: str, user: User = Depends(require_platform_admin), db: Session = Depends(get_db)):
    page = db.get(Page, page_id)
    if not page:
        raise HTTPException(404)
    blocks = sorted(page.blocks, key=lambda b: b.position)
    public_url = "/" if page.slug == "home" else f"/{page.slug}"
    return tpl(request, "admin/page_edit.html", {
        "page": page, "blocks": blocks, "user": user, "public_url": public_url,
    })


@app.post("/admin/pages/{page_id}")
def update_page(
    page_id: str, title: str = Form(...), slug: str = Form(...), nav_label: str = Form(""),
    show_in_nav: str = Form(""), nav_order: int = Form(0), is_published: str = Form(""),
    meta_description: str = Form(""), user: User = Depends(require_platform_admin), db: Session = Depends(get_db),
):
    page = db.get(Page, page_id)
    if not page:
        raise HTTPException(404)
    slug = slug.strip().lower().replace(" ", "-")
    if not page.is_system and slug != page.slug and not is_slug_available(db, slug, exclude_page_id=page.id):
        return RedirectResponse(url=f"/admin/pages/{page_id}/edit?error=slug", status_code=303)
    page.title = title.strip()
    if not page.is_system:
        page.slug = slug
    page.nav_label = nav_label.strip()
    page.show_in_nav = bool(show_in_nav)
    page.nav_order = nav_order
    page.is_published = bool(is_published)
    page.meta_description = meta_description.strip()
    log_audit(db, user.organization_id, user.id, "page.updated", "page", page.id)
    db.commit()
    return RedirectResponse(url=f"/admin/pages/{page_id}/edit", status_code=303)


@app.post("/admin/pages/{page_id}/delete")
def delete_page(page_id: str, user: User = Depends(require_platform_admin), db: Session = Depends(get_db)):
    page = db.get(Page, page_id)
    if not page:
        raise HTTPException(404)
    if page.is_system:
        raise HTTPException(400, "System pages ('home', 'pricing') can't be deleted, only edited.")
    db.delete(page)
    log_audit(db, user.organization_id, user.id, "page.deleted", "page", page_id)
    db.commit()
    return RedirectResponse(url="/admin/pages", status_code=303)


# ---------------------------------------------------------- content blocks -

def _collect_block_data(form, block_type: str) -> dict:
    fields = BLOCK_FIELDS.get(block_type, {})
    return {key: (form.get(key) or "").strip() for key in fields}


@app.post("/admin/pages/{page_id}/blocks")
async def create_block(
    request: Request, page_id: str,
    user: User = Depends(require_platform_admin), db: Session = Depends(get_db),
):
    page = db.get(Page, page_id)
    if not page:
        raise HTTPException(404)
    form = await request.form()
    block_type = form.get("block_type", "")
    if block_type not in BLOCK_FIELDS:
        raise HTTPException(400, "Unknown block type")

    data = _collect_block_data(form, block_type)

    image = form.get("image_upload")
    if block_type == "image_text" and image and getattr(image, "filename", None):
        ext = Path(image.filename).suffix.lower()
        if ext not in ALLOWED_LOGO_EXTENSIONS:
            raise HTTPException(400, "Unsupported image type. Allowed: PNG, JPG, SVG.")
        content = await image.read()
        if len(content) > MAX_CONTENT_IMAGE_SIZE:
            raise HTTPException(400, "Image exceeds the 5 MB limit.")
        safe_name = f"content-{uuid.uuid4().hex}{ext}"
        (CONTENT_DIR / safe_name).write_bytes(content)
        data["image_url"] = f"/content-assets/{safe_name}"

    required = [k for k, req in BLOCK_FIELDS.get(block_type, {}).items() if req]
    if any(not data.get(k) for k in required):
        raise HTTPException(400, f"Missing required field(s) for {block_type}: {', '.join(required)}")

    block = add_block(db, page.id, block_type, data)
    log_audit(db, user.organization_id, user.id, "content_block.created", "content_block", block.id)
    db.commit()
    return RedirectResponse(url=f"/admin/pages/{page_id}/edit", status_code=303)


@app.get("/admin/pages/{page_id}/blocks/{block_id}/edit", response_class=HTMLResponse)
def edit_block(request: Request, page_id: str, block_id: str, user: User = Depends(require_platform_admin), db: Session = Depends(get_db)):
    block = db.get(ContentBlock, block_id)
    if not block or block.page_id != page_id:
        raise HTTPException(404)
    fields = BLOCK_FIELDS.get(block.block_type, {})
    return tpl(request, "admin/block_edit.html", {
        "block": block, "page_id": page_id, "fields": fields, "user": user,
    })


@app.post("/admin/pages/{page_id}/blocks/{block_id}")
async def update_block(request: Request, page_id: str, block_id: str,
                        user: User = Depends(require_platform_admin), db: Session = Depends(get_db)):
    block = db.get(ContentBlock, block_id)
    if not block or block.page_id != page_id:
        raise HTTPException(404)
    form = await request.form()
    data = _collect_block_data(form, block.block_type)

    image = form.get("image_upload")
    if block.block_type == "image_text" and image and getattr(image, "filename", None):
        ext = Path(image.filename).suffix.lower()
        if ext not in ALLOWED_LOGO_EXTENSIONS:
            raise HTTPException(400, "Unsupported image type. Allowed: PNG, JPG, SVG.")
        content = await image.read()
        if len(content) > MAX_CONTENT_IMAGE_SIZE:
            raise HTTPException(400, "Image exceeds the 5 MB limit.")
        safe_name = f"content-{uuid.uuid4().hex}{ext}"
        (CONTENT_DIR / safe_name).write_bytes(content)
        data["image_url"] = f"/content-assets/{safe_name}"
    elif block.block_type == "image_text":
        data["image_url"] = block.data_json.get("image_url", "")  # keep existing image if none uploaded

    required = [k for k, req in BLOCK_FIELDS.get(block.block_type, {}).items() if req]
    if any(not data.get(k) for k in required):
        raise HTTPException(400, f"Missing required field(s): {', '.join(required)}")

    block.data_json = data
    log_audit(db, user.organization_id, user.id, "content_block.updated", "content_block", block.id)
    db.commit()
    return RedirectResponse(url=f"/admin/pages/{page_id}/edit", status_code=303)


@app.post("/admin/pages/{page_id}/blocks/{block_id}/move-up")
def move_block_up(page_id: str, block_id: str, user: User = Depends(require_platform_admin), db: Session = Depends(get_db)):
    block = db.get(ContentBlock, block_id)
    if not block or block.page_id != page_id:
        raise HTTPException(404)
    move_block(db, block, "up")
    db.commit()
    return RedirectResponse(url=f"/admin/pages/{page_id}/edit", status_code=303)


@app.post("/admin/pages/{page_id}/blocks/{block_id}/move-down")
def move_block_down(page_id: str, block_id: str, user: User = Depends(require_platform_admin), db: Session = Depends(get_db)):
    block = db.get(ContentBlock, block_id)
    if not block or block.page_id != page_id:
        raise HTTPException(404)
    move_block(db, block, "down")
    db.commit()
    return RedirectResponse(url=f"/admin/pages/{page_id}/edit", status_code=303)


@app.post("/admin/pages/{page_id}/blocks/{block_id}/toggle")
def toggle_block(page_id: str, block_id: str, user: User = Depends(require_platform_admin), db: Session = Depends(get_db)):
    block = db.get(ContentBlock, block_id)
    if not block or block.page_id != page_id:
        raise HTTPException(404)
    block.is_active = not block.is_active
    db.commit()
    return RedirectResponse(url=f"/admin/pages/{page_id}/edit", status_code=303)


@app.post("/admin/pages/{page_id}/blocks/{block_id}/delete")
def delete_block(page_id: str, block_id: str, user: User = Depends(require_platform_admin), db: Session = Depends(get_db)):
    block = db.get(ContentBlock, block_id)
    if not block or block.page_id != page_id:
        raise HTTPException(404)
    db.delete(block)
    log_audit(db, user.organization_id, user.id, "content_block.deleted", "content_block", block_id)
    db.commit()
    return RedirectResponse(url=f"/admin/pages/{page_id}/edit", status_code=303)


# ---------------------------------------------------- custom page catch-all

# MUST stay the last route registered: FastAPI/Starlette match routes in
# registration order, so every explicit route above (dashboard, invoices,
# settings, etc.) always wins. Only requests that don't match anything else
# fall through to here, where we look for a published custom Page with a
# matching slug.
@app.get("/{slug}", response_class=HTMLResponse)
def custom_page(request: Request, slug: str, db: Session = Depends(get_db)):
    page = db.scalar(select(Page).where(Page.slug == slug, Page.is_published.is_(True)))
    if not page or page.is_system:
        raise HTTPException(404)
    return render_public_page(request, db, page)
