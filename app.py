import sys
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

from urllib.parse import quote

from sqlalchemy import func

# --- Global for recent delete cache (only holds 1 most recent deleted record) ---
recently_deleted_submission = None
recently_edited_submission = None  # <-- NEW: stores (subid, previous_data)



app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "ghostfest2025")

# ←— 1) Use the Render disk when RENDER=true —→
if os.environ.get("RENDER") == "true":
    app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:////data/ghostfest.db'
else:
    app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///ghostfest.db'

app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)

# ←— 2) Create your tables once at startup under Gunicorn —→
@app.before_first_request
def initialize_database():
    create_tables()

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
    role = db.Column(db.String(16))  # ADD THIS

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
            return redirect(url_for('admin_login'))
        return view_func(*args, **kwargs)
    return wrap

def log_admin(action, user, detail=""):
    db.session.add(AdminLog(action=action, user=user, detail=detail))
    db.session.commit()

def send_whatsapp_reminder(phone, msg):
    pass

def label_date(entry):
    year = entry.get('year', '')
    month = entry.get('month', '')
    day = entry.get('day', '')
    calendar = entry.get('calendar', '')
    parts = []
    if year: parts.append(f"{year}年")
    if month: parts.append(f"{month}月")
    if day: parts.append(f"{day}日")
    date_str = " ".join(parts)
    if calendar:
        if calendar.lower() == "lunar":
            date_str += "（农历）"
        elif calendar.lower() == "english":
            date_str += "（阳历）"
        else:
            date_str += f"（{calendar}）"
    return date_str.strip()

# ---- INITIALIZATION ----
def create_tables():
    db.create_all()
    # ==============================
    #    ADMIN/OWNER PASSWORDS HERE
    # ==============================
    #    CHANGE THESE PASSWORDS FOR OWNER/ADMIN LOGIN
    # ----------------------------------------------
    #    Username: owner   Password: PrincessRF (role: owner)
    #    Username: Lily    Password: 1228247    (role: admin)
    #    Username: Admin   Password: 994Admin   (role: admin)
    # ----------------------------------------------
    #   (Change the password string if you want to update credentials.)
    # ==============================

    # Add users if not exist, with roles
    if not Owner.query.filter_by(username="owner").first():
        db.session.add(
            Owner(
                username="owner",
                password_hash=generate_password_hash("PrincessRF"),
                role="owner"
            )
        )
    if not Owner.query.filter_by(username="Lily").first():
        db.session.add(
            Owner(
                username="Lily",
                password_hash=generate_password_hash("1228247"),
                role="admin"
            )
        )
    if not Owner.query.filter_by(username="Admin").first():
        db.session.add(
            Owner(
                username="Admin",
                password_hash=generate_password_hash("994Admin"),
                role="admin"
            )
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
        exist = Submission.query.filter(
            Submission.name_cn == name_cn,
            db.func.substr(Submission.phone, -8) == phone[-8:]
        ).first()

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
        if len(results) == 1:
            sub = results[0]
            return redirect(url_for("review", oid=sub.order_id, phone=sub.phone))
        else:
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
    try:
        name_cn, phone = selected.split("|", 1)
    except Exception:
        flash("Invalid selection.", "danger")
        return redirect(url_for("check"))
    sub = Submission.query.filter_by(name_cn=name_cn, phone=phone).first()
    if not sub:
        flash("Record not found.", "danger")
        return redirect(url_for("check"))
    return redirect(url_for("review", oid=sub.order_id, phone=sub.phone))



# =======================
#     ADMIN ROUTES
# =======================

# -----------------------
#     TIME HELPER
# -----------------------
def now_utc8():
    return datetime.utcnow() + timedelta(hours=8)

# -----------------------
#     ADMIN DASHBOARD (UPDATED)
# -----------------------
@app.route("/admin", methods=["GET", "POST"])
@login_required
def admin_dashboard():
    page         = int(request.args.get("page", 1))
    per_page     = int(request.args.get("per_page", 20))
    search       = request.args.get("search", "").strip()
    filter_type  = request.args.get("filter_type", "")
    filter_value = request.args.get("filter_value", "").strip()

    # DEBUG: print some raw entries from the DB to check option values
    print("DEBUG: Sample entries JSON:")
    for (js,) in Submission.query.with_entities(Submission.entries).limit(5):
        print(js, file=sys.stderr)  # print to stderr to not mix with normal logs

    q = Submission.query

    # Custom filter for 'option' with manual filtering and pagination
    if filter_type and filter_value:
        if filter_type == "option":
            all_subs = q.order_by(Submission.date.desc()).all()
            filtered_subs = []
            for sub in all_subs:
                try:
                    entries = json.loads(sub.entries)
                    if any(e.get("option") == filter_value for e in entries):
                        filtered_subs.append(sub)
                except Exception:
                    pass

            total_filtered = len(filtered_subs)
            start = (page - 1) * per_page
            end = start + per_page
            orders = filtered_subs[start:end]

            # Simple pagination class for template compatibility
            class SimplePagination:
                def __init__(self, page, per_page, total):
                    self.page = page
                    self.per_page = per_page
                    self.total = total
                    self.pages = (total + per_page - 1) // per_page
                @property
                def has_prev(self): return self.page > 1
                @property
                def has_next(self): return self.page < self.pages
                @property
                def prev_num(self): return self.page - 1
                @property
                def next_num(self): return self.page + 1
                def iter_pages(self):
                    return range(1, self.pages + 1)

            pagination = SimplePagination(page, per_page, total_filtered)

        elif filter_type == "paid":
            # Filter by paid status
            if filter_value.lower() == "paid":
                q = q.filter(Submission.paid == True)
            elif filter_value.lower() == "unpaid":
                q = q.filter(Submission.paid == False)
            else:
                # Unknown value, no filtering applied
                pass
            q = q.order_by(Submission.date.desc())
            pagination = q.paginate(page=page, per_page=per_page, error_out=False)
            orders = pagination.items

        else:
            like = f"%{filter_value}%"
            if filter_type == "gender":
                q = q.filter(Submission.gender == filter_value)
            elif filter_type == "name":
                q = q.filter(
                    Submission.name_cn.ilike(f"%{filter_value}%") |
                    Submission.name_en.ilike(f"%{filter_value}%") |
                    Submission.entries.like(f'%{filter_value}%')
                )
            elif filter_type == "date":
                q = q.filter(Submission.entries.like(f'%{filter_value}%'))
            else:
                q = q.filter(
                    Submission.order_id.ilike(like) |
                    Submission.name_cn.ilike(like) |
                    Submission.name_en.ilike(like) |
                    Submission.phone.ilike(like)
                )
            q = q.order_by(Submission.date.desc())
            pagination = q.paginate(page=page, per_page=per_page, error_out=False)
            orders = pagination.items

    else:
        # No filter or empty filter: normal pagination
        if search:
            like = f"%{search}%"
            q = q.filter(
                Submission.order_id.ilike(like) |
                Submission.name_cn.ilike(like) |
                Submission.name_en.ilike(like) |
                Submission.phone.ilike(like)
            )
        q = q.order_by(Submission.date.desc())
        pagination = q.paginate(page=page, per_page=per_page, error_out=False)
        orders     = pagination.items

    # enrich each submission with formatted entry dates
    for o in orders:
        try:
            entries = json.loads(o.entries)
            for e in entries:
                e["death_date_label"] = label_date(e)
            o.enriched_entries = entries
        except:
            o.enriched_entries = []

    num_orders  = Submission.query.count()
    total_paid  = db.session.query(db.func.sum(Submission.payment_amount)).filter_by(paid=True).scalar() or 0
    total_order = db.session.query(db.func.sum(Submission.total)).scalar() or 0

    # — 1) Auto‑discover all option labels actually stored in the DB —
    raw_labels = {
        e.get("option", "").strip()
        for (js,) in Submission.query.with_entities(Submission.entries)
        for e in json.loads(js or "[]")
        if e.get("option", "").strip()
    }

    # — helper to normalize ANY gender string —
    def normalize_gender(raw_g):
        rg = raw_g.strip().lower()
        if "男" in raw_g or rg.startswith("m"):
            return "male"
        if "女" in raw_g or rg.startswith("f"):
            return "female"
        return "unknown"

    # — 2) Initialize counters for each discovered option —
    option_stats = {
        label: {"total": 0, "male": 0, "female": 0, "unknown": 0}
        for label in sorted(raw_labels)
    }

    # — 3) Tally up every stored entry —
    for (entry_json,) in Submission.query.with_entities(Submission.entries):
        try:
            entries = json.loads(entry_json)
        except json.JSONDecodeError:
            continue

        for e in entries:
            raw_opt = e.get("option", "").strip()
            # ensure even free‑form options get counted
            option_stats.setdefault(raw_opt, {"total":0,"male":0,"female":0,"unknown":0})
            stat = option_stats[raw_opt]
            stat["total"] += 1

            # use our normalizer instead of a static map
            who = normalize_gender(e.get("gender", ""))
            stat[who] += 1

    filter_values = {
        "order_id":       [r[0] for r in db.session.query(Submission.order_id).distinct()],
        "boat":           [r[0] for r in db.session.query(Submission.boat).distinct()],
        "gender":         [r[0] for r in db.session.query(Submission.gender).distinct()],
        "name":           list({r[0] for r in db.session.query(Submission.name_cn).distinct()}) +
                           list({r[0] for r in db.session.query(Submission.name_en).distinct()}),
        "phone":          [r[0] for r in db.session.query(Submission.phone).distinct()],
        "payment_method": [r[0] for r in db.session.query(Submission.payment_method).distinct()],
        "option":         sorted({
                              e.get("option","")
                              for (js,) in db.session.query(Submission.entries)
                              for e in json.loads(js or "[]")
                            }),
        "date":           sorted({
                              e.get("death_date_label","Notsure")
                              for (js,) in db.session.query(Submission.entries)
                              for e in json.loads(js or "[]")
                            }),
    }

    return render_template("admin.html",
        orders=orders,
        pagination=pagination,
        per_page=per_page,
        page=page,
        search=search,
        filter_type=filter_type,
        filter_value=filter_value,
        filter_values=filter_values,
        pause=SysState.query.first().pause,
        num_orders=num_orders,
        total_paid=total_paid,
        total_order=total_order,
        option_stats=option_stats,
        last_updated=now_utc8().strftime("%Y-%m-%d %I:%M %p"),
        is_owner=is_owner()
    )

# -----------------------
#     AJAX: FULL DASHBOARD REFRESH
# -----------------------
@app.route("/admin/refresh", methods=["GET"])
@login_required
def admin_refresh():
    q = Submission.query.order_by(Submission.date.desc()).all()
    orders_data = []

    for o in q:
        try:
            entries = json.loads(o.entries)
            for e in entries:
                e["death_date_label"] = label_date(e)
        except:
            entries = []

        orders_data.append({
            "id": o.id,
            "order_id": o.order_id,
            "name_cn": o.name_cn,
            "name_en": o.name_en,
            "gender": o.gender,
            "boat": o.boat,
            "phone": o.phone,
            "paid": o.paid,
            "total": o.total,
            "payment_method": o.payment_method,
            "payment_amount": o.payment_amount,
            "remarks": o.remarks,
            "date": o.date.strftime("%Y-%m-%d %H:%M"),
            "entries": entries
        })

    return jsonify({"orders": orders_data})

# -----------------------
#     AJAX: MARK PAID with Total Update
# -----------------------
@app.route("/admin/paid/<int:subid>", methods=["POST"])
@login_required
def admin_mark_paid(subid):
    sub = Submission.query.get_or_404(subid)

    # 1. Get payment_amount from request if provided
    amt = request.form.get("payment_amount")
    if amt is not None:
        try:
            amt = int(amt)
        except Exception:
            amt = 0
        sub.payment_amount = amt

    # 2. FOC logic
    if sub.total == 0:
        sub.paid = True
        if sub.payment_amount is None:
            sub.payment_amount = 0
    else:
        # Toggle paid status as usual for non-RM0
        sub.paid = not sub.paid
        # When marking as unpaid, zero out amount
        if not sub.paid:
            sub.payment_amount = 0

    db.session.commit()

    # 3. Stats: Always sum payment_amount for paid
    total_paid = db.session.query(db.func.sum(Submission.payment_amount)).filter_by(paid=True).scalar() or 0

    return jsonify({
        "ok": True,
        "paid": sub.paid,
        "total_paid": total_paid
    })


# ---- HELPERS ----
# Ensures all entry values are in canonical form for frontend dropdowns
def normalize_entry(e):
    valid_options = ["祖先", "冤亲债主", "无主孤魂", "婴灵", "狗狗"]
    # Option normalization
    opt = e.get("option", "")
    if opt not in valid_options: opt = ""
    # Gender normalization
    g = (e.get("gender") or "").strip().lower()
    if g in ["男", "m", "male"]: g = "male"
    elif g in ["女", "f", "female"]: g = "female"
    else: g = ""
    # Calendar normalization
    cal = (e.get("calendar") or "").strip().lower()
    if cal.startswith("eng"): cal = "english"
    elif cal.startswith("lun"): cal = "lunar"
    elif cal in ["", "not sure", "notsure"]: cal = ""
    else: cal = ""
    return {
        "option": opt,
        "name_cn": e.get("name_cn", ""),
        "gender": g,
        "calendar": cal,
        "year": e.get("year", ""),
        "month": e.get("month", ""),
        "day": e.get("day", ""),
    }

# Ensures properlly defined owner & admin role
def is_owner():
    return session.get('role') == 'owner'

def is_admin():
    return session.get('role') in ['owner', 'admin']


# -----------------------
#     AJAX: EDIT POPUP
# -----------------------
@app.route("/admin/edit/<int:subid>", methods=["GET", "POST"])
@login_required
def admin_edit(subid):
    global recently_edited_submission
    sub = Submission.query.get_or_404(subid)

    if request.method == "POST":

        # Save current state BEFORE overwriting
        recently_edited_submission = {
            "id": sub.id,
            "order_id": sub.order_id,
            "date": sub.date,
            "boat": sub.boat,
            "gender": sub.gender,
            "name_cn": sub.name_cn,
            "name_en": sub.name_en,
            "phone": sub.phone,
            "payment_method": sub.payment_method,
            "count": sub.count,
            "total": sub.total,
            "paid": sub.paid,
            "entries": sub.entries,
            "payment_amount": sub.payment_amount,
            "remarks": sub.remarks,
        }

        # Update basic fields from form data
        for field in ["boat", "gender", "name_cn", "name_en", "phone", "payment_method"]:
            setattr(sub, field, request.form.get(field, getattr(sub, field)))

        # Update count
        sub.count = int(request.form.get("count", sub.count))

        # Always recalculate total amount based on boat & count
        boat = request.form.get("boat", sub.boat)
        free_count = 6 if boat == "yes" else 0
        chargeable = max(0, sub.count - free_count)
        sub.total = chargeable * 38

        # Update other fields
        sub.payment_amount = int(request.form.get("payment_amount", sub.payment_amount or 0))
        sub.remarks = request.form.get("remarks", sub.remarks or "")

        # Collect and validate entries
        ent = []
        for i in range(1, sub.count + 1):
            entry = {
                k: request.form.get(f"d{i}_{k}", "")
                for k in ("option", "name_cn", "gender", "calendar", "year", "month", "day")
            }
            # Prevent blank option from being saved
            if not entry['option']:
                return jsonify({"ok": False, "error": f"Entry {i} option cannot be blank."}), 400
            entry["death_date_label"] = label_date(entry)
            ent.append(entry)

        sub.entries = json.dumps(ent)
        db.session.commit()

        # Log the edit action with the current username from session
        log_admin(
            action="Edit submission",
            user=session.get('username', 'unknown'),
            detail=f"Edited submission ID {subid} with total {sub.total} and count {sub.count}"
        )

        return jsonify({"ok": True})

    # GET request: return current data for modal
    entries = [normalize_entry(e) for e in json.loads(sub.entries)] if sub.entries else []
    print("DEBUG: Entries for modal edit:", entries, file=sys.stderr)
    return jsonify({
        **{f: getattr(sub, f) for f in ("boat", "gender", "name_cn", "name_en", "phone", "payment_method")},
        "count": sub.count,
        "total": sub.total,
        "payment_amount": sub.payment_amount,
        "remarks": sub.remarks,
        "entries": entries
    })


# -----------------------
#     WHATSAPP LINK
# -----------------------
@app.route("/admin/send_reminder/<int:subid>", methods=["POST"])
@login_required
def admin_send_reminder(subid):
    sub = Submission.query.get_or_404(subid)
    current_user = session.get('username', 'unknown')  # get logged-in username or 'unknown'
    if not sub.paid:
        # Compose message with the link and order ID
        base_url = request.host_url.rstrip('/')  # e.g., http://127.0.0.1:5000
        check_link = f"{base_url}/check"
        msg = (
            "您好。Hi. 👋🏻\n"
            "请尽快结清。Kindly settle your payment as soon as possible.\n"
            "To check your payment status and amount, please visit the link below:\n"
            f"👉🏻 {check_link}\n"
            f"Please enter your Order ID: {sub.order_id}\n"
            "谢谢! Thank you! ☺️🙏"
        )
        number = sub.phone.replace("+", "").replace(" ", "")
        link = f"https://wa.me/{number}?text={msg}"

        # Log the reminder action with the current user
        log_admin(
            action="Send WhatsApp reminder",
            user=current_user,
            detail=f"Sent reminder to {sub.phone} for Order ID {sub.order_id}"
        )
        return jsonify({"ok": True, "whatsapp_link": link})
    return jsonify({"ok": False, "msg": "Already paid"})



# -----------------------
#     EXPORT EXCEL
# -----------------------
@app.route("/admin/export/excel")
@login_required
def export_excel():
    orders = Submission.query.all()
    rows = []
    option_bilingual = {
        "祖先": "祖先 (Ancestor)",
        "冤亲债主": "冤亲债主 (Debtors)",
        "无主孤魂": "无主孤魂 (Spirits)",
        "婴灵": "婴灵 (Baby)",
        "狗狗": "狗狗 (Dogs)"
    }
    gender_bilingual = {
        "male": "男 / Male", "female": "女 / Female",
        "男": "男 / Male", "女": "女 / Female",
        "M": "男 / Male", "F": "女 / Female"
    }
    boat_bilingual = {"yes": "是 / Yes", "no": "否 / No"}

    for o in orders:
        boat_val = boat_bilingual.get(str(o.boat).lower(), o.boat)
        gender_val = gender_bilingual.get(str(o.gender).lower(), o.gender)
        pay_method = str(o.payment_method).lower()
        if pay_method == "bank_transfer":
            payment_label = "BANK"
        elif pay_method == "tng":
            payment_label = "TNG"
        else:
            payment_label = o.payment_method.upper()

        base = {
            "Timestamp": o.date.strftime("%Y-%m-%d %H:%M"),
            "Order ID": o.order_id,
            "Boat": boat_val,
            "Gender": gender_val,
            "Chinese Name": o.name_cn,
            "English Name": o.name_en,
            "Phone": o.phone,
            "Payment Method": payment_label,
            "Total": o.total,
            "Paid": "Yes" if o.paid else "No",
            "Paid Amt": o.payment_amount,
            "Remarks": o.remarks
        }
        for idx, e in enumerate(json.loads(o.entries), 1):
            option = e.get("option", "")
            option_label = option_bilingual.get(option, option)
            name = e.get("name_cn", "")
            gender_val = gender_bilingual.get(e.get("gender", ""), e.get("gender", ""))
            gender_bracket = gender_val.split(" ")[0] if gender_val else ""
            death_date = label_date(e)
            base[f"Entry {idx}"] = f"{option_label} - {name} ({gender_bracket}) {death_date}"
        rows.append(base)
    
    df = pd.DataFrame(rows)
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine='xlsxwriter') as w:
        df.to_excel(w, index=False)
    buf.seek(0)

    # Log the export action
    log_admin(
        action="Export Excel",
        user=session.get('username', 'unknown'),
        detail=f"Exported Excel report with {len(orders)} submissions"
    )

    return send_file(
        buf,
        as_attachment=True,
        download_name="2025 - Spirits.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )


# -----------------------
#     OTHER ROUTES
# -----------------------
@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    last_logins = (
        db.session.query(
            AdminLog.user,
            func.max(AdminLog.ts).label("last_login_time")
        )
        .filter(AdminLog.action == "Admin login")
        .group_by(AdminLog.user)
        .all()
    )
    # Convert to dict for easier use in template
    last_login_dict = {user: last_login_time for user, last_login_time in last_logins}

    if request.method == "POST":
        username = request.form["username"]
        password = request.form["password"]
        owner = Owner.query.filter_by(username=username).first()
        if owner and check_password_hash(owner.password_hash, password):
            session['username'] = owner.username
            session['role'] = owner.role  # <-- save role in session

            # Based on role, set admin/owner flags for convenience
            session['is_owner'] = (owner.role == "owner")
            session['is_admin'] = (owner.role in ["owner", "admin"])

            log_admin(
                action="Admin login",
                user=owner.username,
                detail=f"User {owner.username} logged in."
            )

            return redirect(url_for("admin_dashboard"))
        flash("Invalid credentials", "danger")

    return render_template("admin_login.html", last_logins=last_login_dict)


@app.route("/admin/logout")
def admin_logout():
    if 'username' in session:
        log_admin(
            action="Admin logout",
            user=session.get('username', 'unknown'),
            detail=f"User {session.get('username')} logged out."
        )
    session.clear()
    return redirect(url_for("admin_login"))

@app.route("/admin/backup")
@login_required
def backup_db():
    # Log the backup download action with current username
    log_admin(
        action="Backup database",
        user=session.get('username', 'unknown'),
        detail="Downloaded ghostfest.db backup file"
    )
    return send_file(
        "ghostfest.db",
        as_attachment=True,
        download_name="ghostfest.db",
        mimetype="application/octet-stream"
    )

@app.route("/admin/pause", methods=["POST"])
@login_required
def admin_pause():
    if not is_owner():
        abort(403)  # Only owner can pause/resume

    s = SysState.query.first()
    s.pause = not s.pause
    db.session.commit()

    current_user = session.get('username', 'unknown')
    log_admin(
        action="Pause Submissions" if s.pause else "Resume Submissions",
        user=current_user,
        detail=f"Submissions {'paused' if s.pause else 'resumed'} by user."
    )

    return redirect(url_for("admin_dashboard"))


@app.route("/admin/pause/request_approval", methods=["POST"])
@login_required
def admin_pause_request_approval():
    if not is_admin():
        return jsonify({"ok": False, "error": "Only admins can request pause/resume approval."}), 403

    current_user = session.get('username', 'unknown')
    owner_phone = '60165207048'  # Owner WhatsApp number, no plus

    msg = (
        f"Admin {current_user} requests to pause/resume submissions.\n"
        "Please review and approve in the admin dashboard."
    )
    wa_link = f"https://wa.me/{owner_phone}?text={msg}"

    # Log this approval request event
    log_admin(
        action="Request pause/resume approval",
        user=current_user,
        detail="Requested owner approval to pause/resume submissions."
    )

    return jsonify({"ok": True, "whatsapp_link": wa_link})

# -----------------------
#     UNDO EDIT
# -----------------------
@app.route("/admin/undo_edit", methods=["POST"])
@login_required
def admin_undo_edit():
    global recently_edited_submission
    if not recently_edited_submission:
        return jsonify({"ok": False, "error": "No recent edit to undo."})

    sub = Submission.query.get(recently_edited_submission["id"])
    if not sub:
        return jsonify({"ok": False, "error": "Record not found for undo."})

    # Restore all previous fields
    for field in [
        "order_id", "date", "boat", "gender", "name_cn", "name_en", "phone",
        "payment_method", "count", "total", "paid", "entries", "payment_amount", "remarks"
    ]:
        setattr(sub, field, recently_edited_submission[field])
    db.session.commit()
    # ---- Log the undo ----
    current_user = session.get('username', 'unknown')
    log_admin(
        action="Undo Edit",
        user=current_user,
        detail=f"Reverted edit for submission Order ID: {sub.order_id}, Name: {sub.name_cn}"
    )
    recently_edited_submission = None
    return jsonify({"ok": True})

# -----------------------
#     UNDO DELETE
# -----------------------
@app.route("/admin/undo", methods=["POST"])
@login_required
def admin_undo():
    global recently_deleted_submission
    if recently_deleted_submission is None:
        return jsonify({"ok": False, "error": "No recent deletion to undo."})
    existing = Submission.query.filter_by(order_id=recently_deleted_submission["order_id"]).first()
    if existing:
        return jsonify({"ok": False, "error": "Order ID already exists, cannot undo."})
    sub = Submission(
        order_id = recently_deleted_submission["order_id"],
        date = recently_deleted_submission["date"],
        boat = recently_deleted_submission["boat"],
        gender = recently_deleted_submission["gender"],
        name_cn = recently_deleted_submission["name_cn"],
        name_en = recently_deleted_submission["name_en"],
        phone = recently_deleted_submission["phone"],
        payment_method = recently_deleted_submission["payment_method"],
        count = recently_deleted_submission["count"],
        total = recently_deleted_submission["total"],
        paid = recently_deleted_submission["paid"],
        entries = recently_deleted_submission["entries"],
        payment_amount = recently_deleted_submission["payment_amount"],
        remarks = recently_deleted_submission["remarks"],
    )
    db.session.add(sub)
    db.session.commit()
    # ---- Log the undo ----
    current_user = session.get('username', 'unknown')
    log_admin(
        action="Undo Delete",
        user=current_user,
        detail=f"Restored deleted submission Order ID: {sub.order_id}, Name: {sub.name_cn}"
    )
    recently_deleted_submission = None
    return jsonify({"ok": True})

# -----------------------
#     OWNER WHATSAPP ENDPOINT
# -----------------------
@app.route("/admin/owner_whatsapp")
@login_required
def admin_owner_whatsapp():
    # Hardcoded WhatsApp number for owner (no +, just country+number)
    owner_phone = '60165207048'
    return jsonify({"ok": True, "phone": owner_phone})

# -----------------------
#     DELETE SUBMISSION (OWNER ONLY)
# -----------------------
@app.route("/admin/delete/<int:subid>", methods=["POST"])
@login_required
def admin_delete(subid):
    global recently_deleted_submission
    if not is_owner():
        return jsonify({"ok": False, "error": "Only the owner can delete. Ask owner for approval."}), 403
    sub = Submission.query.get_or_404(subid)   # <-- THIS WAS MISSING
    # Save deleted record in cache
    recently_deleted_submission = {
        "id": sub.id,
        "order_id": sub.order_id,
        "date": sub.date,
        "boat": sub.boat,
        "gender": sub.gender,
        "name_cn": sub.name_cn,
        "name_en": sub.name_en,
        "phone": sub.phone,
        "payment_method": sub.payment_method,
        "count": sub.count,
        "total": sub.total,
        "paid": sub.paid,
        "entries": sub.entries,
        "payment_amount": sub.payment_amount,
        "remarks": sub.remarks,
    }
    db.session.delete(sub)
    db.session.commit()
    return jsonify({"ok": True})

# Route to request delete approval (for admins)
@app.route("/admin/request_delete_approval/<int:subid>", methods=["POST"])
@login_required
def admin_request_delete_approval(subid):
    if is_owner():
        return jsonify({"ok": False, "error": "Owner can delete directly."}), 400

    sub = Submission.query.get_or_404(subid)
    current_user = session.get('username', 'unknown')
    owner_phone = '60165207048'  # Owner's WhatsApp number without '+'

    msg = (
        f"Admin {current_user} requests to DELETE submission:\n"
        f"Order ID: {sub.order_id}, Name: {sub.name_cn}\n"
        f"Please approve or reject in the admin dashboard."
    )
    wa_link = f"https://wa.me/{owner_phone}?text={quote(msg)}"

    log_admin(
        action="Request delete approval",
        user=current_user,
        detail=f"Requested owner approval to delete submission ID {subid} (Order ID {sub.order_id})"
    )

    return jsonify({"ok": True, "whatsapp_link": wa_link})

# -----------------------
#   ADMIN HISTORY ROUTE
# -----------------------
@app.route("/admin/history")
@login_required
def admin_history():
    logs = AdminLog.query.order_by(AdminLog.ts.desc()).limit(200).all()
    return render_template("admin_history.html", logs=logs)



# =======================
#   END ADMIN ROUTES
# =======================

# (No debug_options route)

# ←— 3) Health‑check endpoint for Render —→
@app.route("/healthz")
def health_check():
    return "OK"

if __name__ == "__main__":
    with app.app_context():
        create_tables()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)

