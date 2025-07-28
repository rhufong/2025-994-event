from flask import Blueprint, render_template, request, redirect, url_for, session, flash, jsonify, abort, send_file
import json
import pandas as pd
import io
from fpdf import FPDF
from werkzeug.security import check_password_hash

# Import all needed from app.py — only do this ONCE at the top!
from app import db, Submission, Owner, SysState, AdminLog, now_utc8, login_required, log_admin, is_owner, is_admin, send_whatsapp_reminder

admin_bp = Blueprint('admin_bp', __name__)

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

# ------- ADMIN ROUTES --------

@admin_bp.route("/admin", methods=["GET", "POST"])
@login_required
def admin_dashboard():
    q = Submission.query
    filter_type = request.args.get("filter_type")
    filter_value = request.args.get("filter_value")
    search = request.args.get("search")
    sys_state = SysState.query.first()
    pause = sys_state.pause if sys_state else False

    if filter_type and filter_value:
        if filter_type == "option":
            q = q.filter(Submission.entries.like(f'%"{filter_value}"%'))
        elif filter_type == "gender":
            q = q.filter(Submission.gender == filter_value)
    if search:
        q = q.filter(
            Submission.name_cn.like(f"%{search}%")
            | Submission.name_en.like(f"%{search}%")
            | Submission.phone.like(f"%{search}%")
            | Submission.order_id.like(f"%{search}%"))
    orders = q.order_by(Submission.date.desc()).all()

    # Enrich entries with labeled death date for visual in All Entries column
    for o in orders:
        try:
            entries = json.loads(o.entries)
            for e in entries:
                e["death_date_label"] = label_date(e)
            o.enriched_entries = entries
        except Exception:
            o.enriched_entries = []
    num_orders = Submission.query.count()
    total_paid = db.session.query(db.func.sum(
        Submission.total)).filter_by(paid=True).scalar() or 0
    total_order = db.session.query(db.func.sum(Submission.total)).scalar() or 0
    last_updated = now_utc8().strftime("%Y-%m-%d %H:%M")

    return render_template("admin.html",
                           orders=orders,
                           num_orders=num_orders,
                           total_paid=total_paid,
                           total_order=total_order,
                           pause=pause,
                           last_updated=last_updated,
                           is_owner=is_owner())

@admin_bp.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        username = request.form.get("username")
        password = request.form.get("password")
        owner = Owner.query.filter_by(username=username).first()
        if owner and check_password_hash(owner.password_hash, password):
            session['is_owner'] = True
            session['is_admin'] = True
            return redirect(url_for("admin_bp.admin_dashboard"))
        flash("Invalid credentials", "danger")
    return render_template("admin_login.html")

@admin_bp.route("/admin/logout")
def admin_logout():
    session.clear()
    return redirect(url_for("admin_bp.admin_login"))

@admin_bp.route("/admin/pause", methods=["POST"])
@login_required
def admin_pause():
    if not is_owner():
        abort(403)
    sys_state = SysState.query.first()
    sys_state.pause = not sys_state.pause
    db.session.commit()
    log_admin("toggle_pause", "owner", f"Pause set to {sys_state.pause}")
    return redirect(url_for("admin_bp.admin_dashboard"))

@admin_bp.route("/admin/edit/<int:subid>", methods=["GET", "POST"])
@login_required
def admin_edit(subid):
    sub = Submission.query.get_or_404(subid)
    if request.method == "POST":
        sub.boat = request.form.get("boat", sub.boat)
        sub.gender = request.form.get("gender", sub.gender)
        sub.name_cn = request.form.get("name_cn", sub.name_cn)
        sub.name_en = request.form.get("name_en", sub.name_en)
        sub.phone = request.form.get("phone", sub.phone)
        sub.payment_method = request.form.get("payment_method", sub.payment_method)
        sub.count = int(request.form.get("count", sub.count))
        sub.total = int(request.form.get("total", sub.total))
        sub.payment_amount = int(request.form.get("payment_amount", sub.payment_amount or 0))
        sub.remarks = request.form.get("remarks", sub.remarks or "")
        entries = []
        for i in range(1, sub.count + 1):
            entries.append({
                "option": request.form.get(f"d{i}_option", ""),
                "name_cn": request.form.get(f"d{i}_name_cn", ""),
                "gender": request.form.get(f"d{i}_gender", ""),
                "calendar": request.form.get(f"d{i}_calendar", ""),
                "year": request.form.get(f"d{i}_year", ""),
                "month": request.form.get(f"d{i}_month", ""),
                "day": request.form.get(f"d{i}_day", ""),
            })
        sub.entries = json.dumps(entries)
        db.session.commit()
        log_admin("edit", session.get("user", "admin"), f"Edit {sub.id}")
        return jsonify({"ok": True})
    return jsonify({
        "boat": sub.boat,
        "gender": sub.gender,
        "name_cn": sub.name_cn,
        "name_en": sub.name_en,
        "phone": sub.phone,
        "payment_method": sub.payment_method,
        "count": sub.count,
        "total": sub.total,
        "payment_amount": sub.payment_amount,
        "remarks": sub.remarks,
        "entries": json.loads(sub.entries),
    })

@admin_bp.route("/admin/paid/<int:subid>", methods=["POST"])
@login_required
def admin_mark_paid(subid):
    sub = Submission.query.get_or_404(subid)
    sub.paid = not sub.paid
    db.session.commit()
    log_admin("mark_paid", session.get("user", "admin"),
              f"Mark paid {sub.id} -> {sub.paid}")
    return jsonify({"ok": True, "paid": sub.paid})

@admin_bp.route("/admin/delete/<int:subid>", methods=["POST"])
@login_required
def admin_delete(subid):
    if not is_owner():
        send_whatsapp_reminder("+60165207048", f"Admin requests delete for {subid}. Approve?")
        return jsonify({"ok": False, "msg": "Request sent to owner"})
    sub = Submission.query.get_or_404(subid)
    db.session.delete(sub)
    db.session.commit()
    log_admin("delete", "owner", f"Deleted {subid}")
    return jsonify({"ok": True})

@admin_bp.route("/admin/export/excel")
@login_required
def export_excel():
    orders = Submission.query.all()
    data = []
    for o in orders:
        d = {
            "Timestamp": o.date.strftime("%Y-%m-%d %H:%M"),
            "Order ID": o.order_id,
            "Boat": o.boat,
            "Gender": o.gender,
            "Chinese Name": o.name_cn,
            "English Name": o.name_en,
            "Phone": o.phone,
            "Payment Method": o.payment_method,
            "Total": o.total,
            "Paid": "Yes" if o.paid else "No",
            "Payment Amount": o.payment_amount,
            "Remarks": o.remarks,
        }
        entries = json.loads(o.entries)
        for idx, entry in enumerate(entries, 1):
            d[f"Entry {idx}"] = f"{entry.get('option')} - {entry.get('name_cn')} ({entry.get('gender')}) " \
                f"{label_date(entry)}"
        data.append(d)
    df = pd.DataFrame(data)
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='xlsxwriter') as writer:
        df.to_excel(writer, index=False)
    output.seek(0)
    return send_file(
        output,
        as_attachment=True,
        download_name="ghostfest.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )

@admin_bp.route("/admin/export/pdf")
@login_required
def export_pdf():
    orders = Submission.query.all()
    pdf = FPDF('L', 'mm', 'A4')
    pdf.set_auto_page_break(auto=True, margin=10)
    pdf.add_page()
    pdf.set_font("Arial", size=10)
    col_names = [
        "Timestamp", "Order ID", "Boat", "Gender", "Chinese Name",
        "English Name", "Phone", "Payment Method", "Total", "Paid",
        "Payment Amount", "Remarks"
    ]
    for col in col_names:
        pdf.cell(35, 10, col, 1, 0, 'C')
    pdf.ln()
    for o in orders:
        row = [
            o.date.strftime("%Y-%m-%d %H:%M"), o.order_id, o.boat, o.gender,
            o.name_cn, o.name_en, o.phone, o.payment_method,
            str(o.total), "Yes" if o.paid else "No",
            str(o.payment_amount), o.remarks or ""
        ]
        for col in row:
            pdf.cell(35, 10, str(col)[:32], 1, 0, 'C')
        pdf.ln()
    output = io.BytesIO()
    pdf.output(output)
    output.seek(0)
    return send_file(output,
                     as_attachment=True,
                     download_name="ghostfest.pdf",
                     mimetype="application/pdf")

@admin_bp.route("/admin/backup")
@login_required
def backup_db():
    return send_file("ghostfest.db",
                     as_attachment=True,
                     download_name="ghostfest.db",
                     mimetype="application/octet-stream")

@admin_bp.route("/admin/undo", methods=["POST"])
@login_required
def admin_undo():
    last = AdminLog.query.order_by(AdminLog.ts.desc()).first()
    if last and last.action in ["delete", "edit"]:
        # Implement undo logic here if you want
        pass
    return jsonify({"ok": True})

@admin_bp.route("/admin/history")
@login_required
def admin_history():
    logs = AdminLog.query.order_by(AdminLog.ts.desc()).limit(100).all()
    return render_template("admin_history.html", logs=logs)
