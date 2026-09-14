"""Seed realistic demo data.

Run with:  python -m app.seed
"""
import random
from datetime import date, timedelta

from app.db import Base, engine, session_scope
from app.models import Organization, User, Vendor, Invoice, Subscription
from app.security import hash_password
from app.services import ValidationEngine, get_ai_provider, ensure_default_rules

VENDOR_NAMES = [
    "Northwind Office Supply", "Blue Ridge Logistics", "Apex Cloud Hosting",
    "Meridian Legal Group", "Cascade Facilities Mgmt", "Union Steel Fabrication",
    "Harborlight Marketing", "Redstone Consulting", "Wavecrest IT Services",
    "Granite Peak Construction", "Silverline Catering", "Ferro Industrial Parts",
    "Brightwell Insurance", "Oakmont Printing Co", "Delta Fleet Rental",
]


def run():
    Base.metadata.create_all(bind=engine)
    with session_scope() as db:
        existing = db.query(Organization).filter(Organization.slug == "demo-co").first()
        if existing:
            print("Demo data already exists. Skipping seed.")
            return

        org = Organization(name="Demo Co", slug="demo-co", email="admin@example.com", plan="professional")
        db.add(org)
        db.flush()
        db.add(Subscription(organization_id=org.id, plan="professional", status="active"))
        ensure_default_rules(db, org.id)
        db.flush()

        admin = User(
            organization_id=org.id, email="admin@example.com",
            password_hash=hash_password("ChangeMe123!"), first_name="Demo", last_name="Admin",
            role="owner", is_platform_admin=True,
        )
        db.add(admin)

        for i, (first, role) in enumerate([
            ("Jamie", "admin"), ("Priya", "manager"), ("Chris", "reviewer"), ("Sam", "viewer"),
        ]):
            db.add(User(
                organization_id=org.id, email=f"{first.lower()}@example.com",
                password_hash=hash_password("ChangeMe123!"), first_name=first, role=role,
            ))

        vendors = []
        for name in VENDOR_NAMES:
            v = Vendor(
                organization_id=org.id, name=name,
                vendor_number=f"V-{random.randint(1000,9999)}",
                email=f"billing@{name.lower().replace(' ','')[:12]}.com",
                payment_terms=random.choice(["Net 15", "Net 30", "Net 45"]),
                status="active" if random.random() > 0.08 else "inactive",
            )
            db.add(v)
            vendors.append(v)
        db.flush()

        engine_ = ValidationEngine(db, org.id)
        ai = get_ai_provider()

        used_invoice_numbers = set()
        for i in range(100):
            vendor = random.choice(vendors)
            subtotal = round(random.uniform(150, 15000), 2)
            tax = round(subtotal * 0.0825, 2)
            total = round(subtotal + tax, 2)
            # Occasionally introduce a duplicate invoice number to create real findings
            if used_invoice_numbers and random.random() < 0.08:
                inv_num = random.choice(list(used_invoice_numbers))
            else:
                inv_num = f"INV-{10000+i}"
                used_invoice_numbers.add(inv_num)

            inv_date = date.today() - timedelta(days=random.randint(0, 75))
            invoice = Invoice(
                organization_id=org.id,
                vendor_id=vendor.id,
                invoice_number=inv_num,
                invoice_date=inv_date,
                due_date=inv_date + timedelta(days=30),
                po_number=f"PO-{random.randint(2000,2999)}" if random.random() > 0.15 else "",
                subtotal=subtotal, tax=tax, total=total,
                source="upload",
            )
            db.add(invoice)
            db.flush()

            findings, score = engine_.run(invoice)
            for f in findings:
                f.invoice_id = invoice.id
                db.add(f)
            analysis = ai.analyze_invoice(invoice, findings, score)
            invoice.risk_score = score
            invoice.risk_level = analysis["risk_level"]
            invoice.ai_summary = analysis["summary"]
            if score >= 30:
                invoice.status = "needs_review"
                invoice.approval_status = random.choice(["pending", "pending", "approved", "rejected"])
                if invoice.approval_status != "pending":
                    invoice.status = invoice.approval_status
            else:
                invoice.status = "approved"
                invoice.approval_status = "approved"

        print("Seeded: 1 organization, 5 users, 15 vendors, 100 invoices.")
        print("Login: admin@example.com / ChangeMe123!  (change this password immediately)")


if __name__ == "__main__":
    run()
