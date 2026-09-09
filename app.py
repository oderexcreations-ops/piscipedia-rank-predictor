import csv
import os
import re
import secrets
import smtplib
import sqlite3
from datetime import datetime, timezone
from email.message import EmailMessage
from functools import wraps
from io import StringIO
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, Response, flash, redirect, render_template, request, session, url_for
from PyPDF2 import PdfReader

from seed_data import RECORDS

BASE = Path(__file__).resolve().parent
load_dotenv(BASE / ".env")


def _path_from_env(name: str, default: str) -> Path:
    value = os.getenv(name, default).strip()
    path = Path(value).expanduser()
    return path if path.is_absolute() else BASE / path


DB_PATH = _path_from_env("DATABASE_PATH", "piscipedia.db")
UPLOAD_DIR = _path_from_env("UPLOAD_DIR", "uploads")
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

app = Flask(__name__)
app.config.update(
    SECRET_KEY=os.getenv("SECRET_KEY") or "piscipedia-development-secret-change-this-in-render",
    MAX_CONTENT_LENGTH=10 * 1024 * 1024,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
)

CATEGORIES = ["General / UR", "OBC-NCL", "EWS", "SC", "ST", "Other"]
STATES = [
    "Andhra Pradesh", "Arunachal Pradesh", "Assam", "Bihar", "Chhattisgarh", "Goa",
    "Gujarat", "Haryana", "Himachal Pradesh", "Jharkhand", "Karnataka", "Kerala",
    "Madhya Pradesh", "Maharashtra", "Manipur", "Meghalaya", "Mizoram", "Nagaland",
    "Odisha", "Punjab", "Rajasthan", "Sikkim", "Tamil Nadu", "Telangana", "Tripura",
    "Uttar Pradesh", "Uttarakhand", "West Bengal", "Andaman and Nicobar Islands",
    "Chandigarh", "Dadra and Nagar Haveli and Daman and Diu", "Delhi", "Jammu and Kashmir",
    "Ladakh", "Lakshadweep", "Puducherry",
]

SCHEMA = """
CREATE TABLE IF NOT EXISTS datasets (
    id INTEGER PRIMARY KEY,
    version INTEGER UNIQUE NOT NULL,
    name TEXT NOT NULL,
    source_document TEXT NOT NULL,
    record_count INTEGER NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    activated_at TEXT
);
CREATE TABLE IF NOT EXISTS historical_records (
    id INTEGER PRIMARY KEY,
    marks REAL NOT NULL,
    rank INTEGER NOT NULL,
    category TEXT,
    domicile_state TEXT,
    exam_year INTEGER,
    program TEXT,
    source_document TEXT,
    source_page INTEGER,
    dataset_version INTEGER NOT NULL,
    verified INTEGER DEFAULT 1,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS submissions (
    id INTEGER PRIMARY KEY,
    candidate_name TEXT NOT NULL,
    marks REAL NOT NULL,
    category TEXT NOT NULL,
    other_category TEXT,
    domicile_state TEXT NOT NULL,
    predicted_rank REAL NOT NULL,
    rank_lower REAL NOT NULL,
    rank_upper REAL NOT NULL,
    confidence TEXT NOT NULL,
    match_count INTEGER NOT NULL,
    algorithm_version TEXT NOT NULL,
    dataset_version INTEGER NOT NULL,
    consent INTEGER NOT NULL,
    email_status TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS verified_results (
    id INTEGER PRIMARY KEY,
    submission_id INTEGER UNIQUE NOT NULL,
    actual_rank INTEGER NOT NULL,
    verification_source TEXT,
    notes TEXT,
    verified_at TEXT NOT NULL,
    verified_by TEXT
);
CREATE TABLE IF NOT EXISTS states (
    id INTEGER PRIMARY KEY,
    state_name TEXT UNIQUE NOT NULL,
    state_code TEXT,
    active INTEGER DEFAULT 1
);
"""


def db() -> sqlite3.Connection:
    connection = sqlite3.connect(DB_PATH, timeout=20)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout=20000")
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def init_db() -> None:
    connection = db()
    try:
        connection.executescript(SCHEMA)
        connection.execute("PRAGMA journal_mode=WAL")
        if connection.execute("SELECT COUNT(*) AS n FROM states").fetchone()["n"] == 0:
            connection.executemany(
                "INSERT INTO states(state_name,state_code) VALUES(?,?)",
                [(state, "") for state in STATES],
            )

        if connection.execute("SELECT COUNT(*) AS n FROM datasets").fetchone()["n"] == 0:
            now = datetime.now(timezone.utc).isoformat()
            connection.execute(
                """INSERT INTO datasets
                (version,name,source_document,record_count,status,created_at,activated_at)
                VALUES(?,?,?,?,?,?,?)""",
                (1, "Piscipedia Historical Dataset", "ICAR_AIEEA_PG_2026_Final_Ranks.pdf", len(RECORDS), "active", now, now),
            )
            connection.executemany(
                """INSERT INTO historical_records
                (marks,rank,program,source_document,dataset_version,created_at)
                VALUES(?,?,?,?,?,?)""",
                [(marks, rank, "Fisheries", "ICAR_AIEEA_PG_2026_Final_Ranks.pdf", 1, now) for marks, rank in RECORDS],
            )
        connection.commit()
    finally:
        connection.close()


def active_dataset_version(connection: sqlite3.Connection):
    row = connection.execute(
        "SELECT version FROM datasets WHERE status='active' ORDER BY version DESC LIMIT 1"
    ).fetchone()
    return int(row["version"]) if row else None


def admin_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if not session.get("admin"):
            return redirect(url_for("admin_login"))
        return view(*args, **kwargs)
    return wrapper


def predict_rank(marks: float):
    connection = db()
    try:
        version = active_dataset_version(connection)
        if version is None:
            return None
        rows = connection.execute(
            "SELECT marks,rank FROM historical_records WHERE dataset_version=? ORDER BY marks DESC",
            (version,),
        ).fetchall()
    finally:
        connection.close()

    if not rows:
        return None

    exact = [row for row in rows if abs(float(row["marks"]) - marks) < 1e-9]
    if exact:
        rank = float(exact[0]["rank"])
        return rank, max(1, round(rank - 5)), round(rank + 5), "High", min(10, len(exact)), version

    above = [row for row in rows if float(row["marks"]) > marks]
    below = [row for row in rows if float(row["marks"]) < marks]
    nearest = sorted(rows, key=lambda row: abs(float(row["marks"]) - marks))[:8]

    if above and below:
        high = min(above, key=lambda row: float(row["marks"]) - marks)
        low = max(below, key=lambda row: marks - float(row["marks"]))
        x1, x2 = float(high["marks"]), float(low["marks"])
        y1, y2 = float(high["rank"]), float(low["rank"])
        if x1 != x2:
            rank = y1 + (marks - x1) * (y2 - y1) / (x2 - x1)
        else:
            rank = y1
    else:
        rank = float(nearest[0]["rank"])

    ranks = [float(row["rank"]) for row in nearest]
    spread = max(ranks) - min(ranks) if ranks else 20
    distance = abs(float(nearest[0]["marks"]) - marks)
    confidence = "High" if len(rows) >= 30 and distance <= 2 else ("Moderate" if len(rows) >= 10 and distance <= 8 else "Low")
    half = max(10, round(spread * 0.35))
    lower = max(1, round(rank - half))
    upper = max(lower, round(rank + half))
    return round(rank), lower, upper, confidence, len(nearest), version


def send_notification(data) -> str:
    host = os.getenv("SMTP_HOST", "").strip()
    if not host:
        return "not_configured"

    recipient = os.getenv("NOTIFICATION_EMAIL", "").strip()
    username = os.getenv("SMTP_USERNAME", "").strip()
    password = os.getenv("SMTP_PASSWORD", "")
    sender = username or recipient
    if not recipient or not sender:
        return "not_configured"

    message = EmailMessage()
    message["Subject"] = "New Piscipedia ICAR AIEEA PG Rank Prediction"
    message["From"] = sender
    message["To"] = recipient
    message.set_content("\n".join(f"{key}: {value}" for key, value in data.items()))

    with smtplib.SMTP(host, int(os.getenv("SMTP_PORT", "587")), timeout=10) as smtp:
        if os.getenv("SMTP_USE_TLS", "true").lower() == "true":
            smtp.starttls()
        if username and password:
            smtp.login(username, password)
        smtp.send_message(message)
    return "sent"


@app.get("/")
def home():
    return render_template("home.html")


@app.get("/healthz")
def healthz():
    try:
        connection = db()
        connection.execute("SELECT 1").fetchone()
        connection.close()
        return {"status": "ok"}, 200
    except Exception:
        return {"status": "error"}, 503


@app.route("/predict", methods=["GET", "POST"])
def predict_page():
    if request.method == "GET":
        return render_template("predict.html", categories=CATEGORIES, states=STATES)

    form = request.form
    name = form.get("name", "").strip()
    category = form.get("category", "")
    other_category = form.get("other_category", "").strip()
    state = form.get("state", "")
    consent = form.get("consent")

    try:
        marks = float(form.get("marks", ""))
    except (TypeError, ValueError):
        marks = None

    max_marks = float(os.getenv("MAX_MARKS", "400"))
    error = None
    if not name:
        error = "Please enter your name."
    elif marks is None or not (marks == marks):
        error = "Please enter valid ICAR AIEEA PG marks."
    elif marks < 0 or marks > max_marks:
        error = f"Please enter marks between 0 and {max_marks:g}."
    elif category not in CATEGORIES:
        error = "Please select your category."
    elif category == "Other" and not other_category:
        error = "Please specify your category."
    elif state not in STATES:
        error = "Please select your domicile state/UT."
    elif not consent:
        error = "Please accept the consent statement."

    if error:
        return render_template("predict.html", categories=CATEGORIES, states=STATES, error=error), 400

    result = predict_rank(marks)
    if not result:
        return render_template(
            "predict.html", categories=CATEGORIES, states=STATES,
            error="There is not enough historical data to calculate a prediction.",
        ), 503

    rank, lower, upper, confidence, match_count, dataset_version = result
    now = datetime.now(timezone.utc).isoformat()
    connection = db()
    try:
        cursor = connection.execute(
            """INSERT INTO submissions
            (candidate_name,marks,category,other_category,domicile_state,predicted_rank,
             rank_lower,rank_upper,confidence,match_count,algorithm_version,dataset_version,
             consent,email_status,created_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (name, marks, category, other_category, state, rank, lower, upper, confidence,
             match_count, "historical-interpolation-v2", dataset_version, 1, "pending", now),
        )
        submission_id = cursor.lastrowid
        connection.commit()
    finally:
        connection.close()

    email_status = "not_configured"
    try:
        email_status = send_notification({
            "Submission ID": submission_id,
            "Candidate Name": name,
            "Marks": marks,
            "Category": category,
            "Domicile": state,
            "Predicted Rank": rank,
            "Rank Range": f"{lower}–{upper}",
            "Confidence": confidence,
            "Historical Records Used": match_count,
            "Dataset Version": dataset_version,
            "Algorithm Version": "historical-interpolation-v2",
            "Timestamp": now,
        })
    except Exception:
        # Email must never prevent a candidate from receiving a result.
        email_status = "failed"

    connection = db()
    try:
        connection.execute("UPDATE submissions SET email_status=? WHERE id=?", (email_status, submission_id))
        connection.commit()
    finally:
        connection.close()

    return redirect(url_for("result", sid=submission_id))


@app.get("/result/<int:sid>")
def result(sid):
    connection = db()
    try:
        row = connection.execute("SELECT * FROM submissions WHERE id=?", (sid,)).fetchone()
    finally:
        connection.close()
    if row is None:
        return "Result not found", 404
    return render_template("result.html", r=row)


@app.get("/how-it-works")
def how():
    return render_template("how.html")


@app.get("/faq")
def faq():
    return render_template("faq.html")


@app.get("/about")
def about():
    return render_template("about.html")


@app.get("/privacy")
def privacy():
    return render_template("privacy.html")


@app.get("/terms")
def terms():
    return render_template("terms.html")


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        email = request.form.get("email", "")
        password = request.form.get("password", "")
        admin_email = os.getenv("ADMIN_EMAIL", "admin@example.com")
        admin_password = os.getenv("ADMIN_PASSWORD", "change-me")
        if secrets.compare_digest(email, admin_email) and secrets.compare_digest(password, admin_password):
            session.clear()
            session["admin"] = True
            return redirect(url_for("admin"))
        flash("Invalid administrator credentials.", "error")
    return render_template("admin_login.html")


@app.get("/admin/logout")
def admin_logout():
    session.clear()
    return redirect(url_for("home"))


@app.get("/admin")
@admin_required
def admin():
    connection = db()
    try:
        stats = {
            "predictions": connection.execute("SELECT COUNT(*) AS n FROM submissions").fetchone()["n"],
            "historical": connection.execute("SELECT COUNT(*) AS n FROM historical_records").fetchone()["n"],
            "verified": connection.execute("SELECT COUNT(*) AS n FROM verified_results").fetchone()["n"],
            "datasets": connection.execute("SELECT COUNT(*) AS n FROM datasets").fetchone()["n"],
        }
        submissions = connection.execute(
            """SELECT s.*,v.actual_rank FROM submissions s
               LEFT JOIN verified_results v ON v.submission_id=s.id
               ORDER BY s.id DESC LIMIT 100"""
        ).fetchall()
        datasets = connection.execute("SELECT * FROM datasets ORDER BY version DESC").fetchall()
    finally:
        connection.close()
    return render_template("admin.html", stats=stats, subs=submissions, datasets=datasets)


@app.post("/admin/upload")
@admin_required
def upload_pdf():
    file = request.files.get("pdf")
    if not file or not file.filename or not file.filename.lower().endswith(".pdf"):
        flash("Please upload a PDF file.", "error")
        return redirect(url_for("admin"))

    safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", Path(file.filename).name)
    path = UPLOAD_DIR / safe_name
    file.save(path)

    try:
        reader = PdfReader(str(path))
        text = "\n".join(page.extract_text() or "" for page in reader.pages)
        pairs = [(float(marks), int(rank)) for marks, rank in re.findall(r"(?m)(\d+(?:\.\d+)?)\s+(\d+)\b", text)]
        pairs = list(dict.fromkeys(pairs))
        if not pairs:
            raise ValueError("No score/rank pairs detected in the PDF.")

        connection = db()
        try:
            new_version = (connection.execute("SELECT COALESCE(MAX(version),0) AS m FROM datasets").fetchone()["m"] or 0) + 1
            now = datetime.now(timezone.utc).isoformat()
            connection.execute("UPDATE datasets SET status='inactive' WHERE status='active'")
            connection.execute(
                """INSERT INTO datasets(version,name,source_document,record_count,status,created_at,activated_at)
                   VALUES(?,?,?,?,?,?,?)""",
                (new_version, f"Piscipedia Dataset v{new_version}", safe_name, len(pairs), "active", now, now),
            )
            connection.executemany(
                """INSERT INTO historical_records
                   (marks,rank,program,source_document,dataset_version,created_at)
                   VALUES(?,?,?,?,?,?)""",
                [(marks, rank, "Fisheries", safe_name, new_version, now) for marks, rank in pairs],
            )
            connection.commit()
        finally:
            connection.close()
        flash(f"Imported {len(pairs)} records as Dataset v{new_version}.", "success")
    except Exception as exc:
        flash(f"PDF import failed: {exc}", "error")

    return redirect(url_for("admin"))


@app.post("/admin/verify/<int:sid>")
@admin_required
def verify(sid):
    try:
        actual_rank = int(request.form.get("actual_rank", ""))
        if actual_rank < 1:
            raise ValueError
    except ValueError:
        flash("Actual rank must be a positive integer.", "error")
        return redirect(url_for("admin"))

    connection = db()
    try:
        exists = connection.execute("SELECT 1 FROM submissions WHERE id=?", (sid,)).fetchone()
        if not exists:
            flash("Submission not found.", "error")
            return redirect(url_for("admin"))
        connection.execute(
            """INSERT INTO verified_results
               (submission_id,actual_rank,verification_source,notes,verified_at,verified_by)
               VALUES(?,?,?,?,?,?)
               ON CONFLICT(submission_id) DO UPDATE SET
                 actual_rank=excluded.actual_rank,
                 verification_source=excluded.verification_source,
                 notes=excluded.notes,
                 verified_at=excluded.verified_at,
                 verified_by=excluded.verified_by""",
            (sid, actual_rank, "Administrator", "", datetime.now(timezone.utc).isoformat(), os.getenv("ADMIN_EMAIL", "admin")),
        )
        connection.commit()
    finally:
        connection.close()
    flash("Verified result saved.", "success")
    return redirect(url_for("admin"))


@app.get("/admin/export")
@admin_required
def export_csv():
    connection = db()
    try:
        rows = connection.execute("SELECT * FROM submissions ORDER BY id").fetchall()
    finally:
        connection.close()

    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(rows[0].keys() if rows else ["id"])
    for row in rows:
        writer.writerow(list(row))
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=piscipedia_submissions.csv"},
    )


@app.errorhandler(413)
def too_large(_error):
    return "Uploaded file is too large. Maximum size is 10 MB.", 413


@app.context_processor
def globals_processor():
    return {"brand": "Piscipedia"}


# Render can start this file directly (python app.py), while render.yaml and
# Procfile use Gunicorn. Both paths bind to 0.0.0.0 and Render's $PORT.
if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")), debug=False)
else:
    init_db()
