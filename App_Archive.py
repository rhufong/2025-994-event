import os
import json
import io
from datetime import datetime, timedelta, timezone
from functools import wraps
from flask import (
    Flask, render_template, request, redirect, url_for, flash,
    session, send_file, jsonify, abort
)
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash

import pandas as pd
from fpdf import FPDF

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "ghostfest2025")
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///ghostfest.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)
print("Running app.py from:", os.path.abspath(__file__))
print(">>> ABSOLUTE PATH THIS APP.PY:", os.path.abspath(__file__))

# ---- MODELS ----
class SysState(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    pause = db.Column(db.Boolean, default=False)

class AdminLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    action = db.Column(db.String(128))
    user = db.Column(db.String(32))
    detail = db.Column(db.Text)
    ts = db.Column(db.DateTime, default=lambda: now_utc8())

class Submission(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.String(8))
    date = db.Column(db.DateTime, default=lambda: now_utc8())
    boat = db.Column(db.String(8))
    gender = db.Column(db.String(8))
    name_cn = db.Column(db.String(24))
    name_en = db.Column(db.String(32))
    phone = db.Column(db.String(24))
    payment_method = db.Column(db.String(24))
    count = db.Column(db.Integer)
    total = db.Column(db.Integer)
    paid = db.Column(db.Boolean, default=False)
    entries = db.Column(db.Text)  # JSON string of entries
    payment_amount = db.Column(db.Integer, default=0)  # Admin/owner editable
    remarks = db.Column(db.Text, default="")  # Admin/owner editable

class Owner(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(24), unique=True)
    password_hash = db.Column(db.String(128))

# ---- HELPERS ----
def now_utc8():
    return datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=8)))

def get_local_phone(phone):
    return ''.join(filter(str.isdigit, phone))[-8:]

def is_owner():
    return session.get('is_owner', False)

def is_admin():
    return session.get('is_admin', False) or is_owner()

def login_required(view_func):
    @wraps(view_func)
    def wrap(*args, **kwargs):
        if not is_admin():
            return redirect(url_for('admin_bp.admin_login'))
        return view_func(*args, **kwargs)
    return wrap

def log_admin(action, user, detail=""):
    db.session.add(AdminLog(action=action, user=user, detail=detail))
    db.session.commit()

def send_whatsapp_reminder(phone, msg):
    pass

# ---- INITIALIZATION ----
def create_tables():
    db.create_all()
    # Add owner if not exists
    if not Owner.query.filter_by(username="owner").first():
        db.session.add(
            Owner(username="owner", password_hash=generate_password_hash("PrincessRF"))
        )
    if not Owner.query.filter_by(username="Lily").first():
        db.session.add(
            Owner(username="Lily", password_hash=generate_password_hash("1228247"))
        )
    if not Owner.query.filter_by(username="Admin").first():
        db.session.add(
            Owner(username="Admin", password_hash=generate_password_hash("994Admin"))
        )
    db.session.commit()
    if not SysState.query.first():
        db.session.add(SysState(pause=False))
        db.session.commit()

# ---- USER ROUTES ----
@app.route("/", methods=["GET", "POST"])
def register():
    sys_state = SysState.query.first()
    if sys_state and sys_state.pause:
        return render_template("closed.html")
    if request.method == "POST":
        # Get all form data
        boat = request.form.get("boat", "")
        gender = request.form.get("your_gender", "")
        name_cn = request.form.get("name_cn", "")
        name_en = request.form.get("name_en", "")
        country_code = request.form.get("country_code", "")
        phone = request.form.get("phone", "")
        count = int(request.form.get("count", "1"))
        payment_method = request.form.get("payment_method", "")
        confirm = request.form.get("confirm")
        dup_key = request.form.get("dup_key")
        # Build entries
        entries = []
        for i in range(1, count + 1):
            entries.append({
                "option": request.form.get(f"d{i}_option", ""),
                "name_cn": request.form.get(f"d{i}_name_cn", ""),
                "gender": request.form.get(f"d{i}_gender", ""),
                "calendar": request.form.get(f"d{i}_calendar", ""),
                "year": request.form.get(f"d{i}_year", ""),
                "month": request.form.get(f"d{i}_month", ""),
                "day": request.form.get(f"d{i}_day", ""),
            })
        # Check for duplicate (same name_cn and last 8 digits of phone)
        exist = Submission.query.filter(
            Submission.name_cn == name_cn,
            db.func.substr(Submission.phone, -8) == phone[-8:]
        ).first()

        # --- 1. Overwrite logic if confirm is set ---
        if exist and confirm == "yes":
            exist.boat = boat
            exist.gender = gender
            exist.name_en = name_en
            exist.phone = country_code + phone
            exist.payment_method = payment_method
            exist.count = count
            free_count = 6 if boat == "yes" else 0
            chargeable = max(0, count - free_count)
            exist.total = chargeable * 38
            exist.entries = json.dumps(entries)
            exist.date = now_utc8()
            db.session.commit()
            return redirect(url_for("review", oid=exist.order_id, phone=exist.phone))

        # --- 2. Duplicate found (same chinese name & last 8 digits), ask to confirm overwrite
        if exist and not confirm:
            form_data = {
                "boat": boat,
                "your_gender": gender,
                "name_cn": name_cn,
                "name_en": name_en,
                "country_code": country_code,
                "phone": phone,
                "count": count,
                "payment_method": payment_method
            }
            for i in range(1, count + 1):
                form_data[f"d{i}_option"] = request.form.get(f"d{i}_option", "")
                form_data[f"d{i}_name_cn"] = request.form.get(f"d{i}_name_cn", "")
                form_data[f"d{i}_gender"] = request.form.get(f"d{i}_gender", "")
                form_data[f"d{i}_calendar"] = request.form.get(f"d{i}_calendar", "")
                form_data[f"d{i}_year"] = request.form.get(f"d{i}_year", "")
                form_data[f"d{i}_month"] = request.form.get(f"d{i}_month", "")
                form_data[f"d{i}_day"] = request.form.get(f"d{i}_day", "")
            dup_key = name_cn + "_" + phone[-8:]
            return render_template(
                "confirm.html",
                dup=exist,
                form_data=form_data,
                dup_key=dup_key
            )

        # --- 3. If same last 8 digits but different name_cn: allow as new submission
        # (No need to check, as the above only triggers if both match)

        # --- 4. Normal submission ---
        free_count = 6 if boat == "yes" else 0
        chargeable = max(0, count - free_count)
        total = chargeable * 38
        order_id = phone[-4:]
        sub = Submission(order_id=order_id,
                         boat=boat,
                         gender=gender,
                         name_cn=name_cn,
                         name_en=name_en,
                         phone=country_code + phone,
                         payment_method=payment_method,
                         count=count,
                         total=total,
                         entries=json.dumps(entries))
        db.session.add(sub)
        db.session.commit()
        return redirect(url_for("review", oid=order_id, phone=country_code + phone))
    return render_template("register.html")

@app.route("/review")
def review():
    oid = request.args.get("oid")
    phone = request.args.get("phone")
    if not oid or not phone:
        return redirect(url_for("register"))
    sub = Submission.query.filter_by(order_id=oid, phone=phone).first()
    if not sub:
        flash("Submission not found.", "danger")
        return redirect(url_for("register"))
    entries = json.loads(sub.entries)
    qr_url = None
    if sub.payment_method and sub.payment_method.lower() == "tng":
        qr_url = "/static/tng_qr_code.jpeg"
    elif sub.payment_method and sub.payment_method.lower() == "bank_transfer":
        qr_url = "/static/bank_transfer_qr_code.jpeg"

    return render_template("review.html", order=sub, entries=entries, qr_url=qr_url)

@app.route("/confirm", methods=["POST"])
def confirm():
    return render_template("confirm.html")

@app.route("/check", methods=["GET", "POST"])
def check():
    error = None
    if request.method == "POST":
        order_id = request.form.get("order_id", "").strip()
        if not order_id or not order_id.isdigit() or len(order_id) != 4:
            error = True
            return render_template("check.html", error=error)
        results = Submission.query.filter(Submission.order_id == order_id).all()
        if not results:
            error = True
            return render_template("check.html", error=error)
        # If only 1 result, go straight to review page
        if len(results) == 1:
            sub = results[0]
            return redirect(url_for("review", oid=sub.order_id, phone=sub.phone))
        else:
            # More than one (same last 4 digits for different people), let user select
            entries = [
                {"name_cn": s.name_cn, "phone": s.phone}
                for s in results
            ]
            return render_template("select_entry.html", entries=entries, order_id=order_id)
    return render_template("check.html", error=error)

@app.route("/select_entry", methods=["POST"])
def select_entry():
    selected = request.form.get("select_name")
    if not selected:
        flash("Please select a record.", "danger")
        return redirect(url_for("check"))
    # selected is "name_cn|phone"
    try:
        name_cn, phone = selected.split("|", 1)
    except Exception:
        flash("Invalid selection.", "danger")
        return redirect(url_for("check"))
    # Get by both name_cn and phone
    sub = Submission.query.filter_by(name_cn=name_cn, phone=phone).first()
    if not sub:
        flash("Record not found.", "danger")
        return redirect(url_for("check"))
    return redirect(url_for("review", oid=sub.order_id, phone=sub.phone))

# ---- REGISTER ADMIN ROUTES USING BLUEPRINT ----
from admin_routes import admin_bp   # Import the blueprint
app.register_blueprint(admin_bp)    # Register the blueprint

print("\n==== REGISTERED ROUTES ====")
for rule in app.url_map.iter_rules():
    print(rule)
print("==== END OF ROUTES ====\n")

# ---- RUN ----
if __name__ == "__main__":
    with app.app_context():
        create_tables()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True, use_reloader=False)
