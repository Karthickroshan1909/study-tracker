import csv
import io
import os
import sqlite3
from datetime import date, timedelta

from flask import Flask, Response, g, jsonify, redirect, render_template, request, url_for

app = Flask(__name__)
# Where the database file lives:
# - the DB_PATH app setting wins if it is set;
# - on Azure App Service (WEBSITE_SITE_NAME exists) we use /home/data, the folder
#   that survives restarts (everything else on Azure is wiped when the app restarts);
# - on your own computer the file sits next to app.py.
_here = os.path.dirname(os.path.abspath(__file__))
_default_dir = "/home/data" if os.environ.get("WEBSITE_SITE_NAME") else _here
DB_PATH = os.environ.get("DB_PATH") or os.path.join(_default_dir, "study_tracker.db")
try:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
except OSError:
    DB_PATH = os.path.join(_here, "study_tracker.db")


# ---------- Database helpers ----------
def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    db = sqlite3.connect(DB_PATH)
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS subjects (
            id   INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE
        );

        CREATE TABLE IF NOT EXISTS sessions (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            subject_id INTEGER NOT NULL,
            topic      TEXT,
            minutes    INTEGER NOT NULL CHECK (minutes > 0),
            study_date TEXT NOT NULL,
            FOREIGN KEY (subject_id) REFERENCES subjects(id)
        );

        CREATE TABLE IF NOT EXISTS tasks (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            title    TEXT NOT NULL,
            due_date TEXT,
            done     INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS exams (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            title     TEXT NOT NULL,
            exam_date TEXT NOT NULL
        );
        """
    )
    db.commit()
    db.close()


# ---------- Routes ----------
@app.route("/")
def index():
    db = get_db()

    subjects = db.execute("SELECT * FROM subjects ORDER BY name").fetchall()

    sessions = db.execute(
        """
        SELECT s.id, s.subject_id, sub.name AS subject, s.topic, s.minutes, s.study_date
        FROM sessions s
        JOIN subjects sub ON sub.id = s.subject_id
        ORDER BY s.study_date DESC, s.id DESC
        """
    ).fetchall()

    # Total time per subject
    per_subject = db.execute(
        """
        SELECT sub.name, COALESCE(SUM(s.minutes), 0) AS total
        FROM subjects sub
        LEFT JOIN sessions s ON s.subject_id = sub.id
        GROUP BY sub.id
        ORDER BY total DESC
        """
    ).fetchall()

    # Last 7 days total
    week_start = (date.today() - timedelta(days=6)).isoformat()
    week_total = db.execute(
        "SELECT COALESCE(SUM(minutes), 0) FROM sessions WHERE study_date >= ?",
        (week_start,),
    ).fetchone()[0]

    all_total = db.execute(
        "SELECT COALESCE(SUM(minutes), 0) FROM sessions"
    ).fetchone()[0]

    # Study streak (consecutive days up to today with at least one session)
    days = {
        r["study_date"]
        for r in db.execute("SELECT DISTINCT study_date FROM sessions").fetchall()
    }
    streak, day = 0, date.today()
    while day.isoformat() in days:
        streak += 1
        day -= timedelta(days=1)

    # Minutes per day (used for the 7-day chart and the heatmap)
    by_day = {
        r["study_date"]: r["m"]
        for r in db.execute(
            "SELECT study_date, SUM(minutes) AS m FROM sessions GROUP BY study_date"
        ).fetchall()
    }
    today_d = date.today()
    last7 = []
    for i in range(6, -1, -1):
        d = today_d - timedelta(days=i)
        last7.append({"label": d.strftime("%a"), "minutes": by_day.get(d.isoformat(), 0)})
    heat = []
    for i in range(83, -1, -1):
        d = today_d - timedelta(days=i)
        heat.append({"date": d.isoformat(), "minutes": by_day.get(d.isoformat(), 0)})

    # Exam countdown
    exams = []
    for e in db.execute("SELECT * FROM exams ORDER BY exam_date").fetchall():
        left = (date.fromisoformat(e["exam_date"]) - today_d).days
        exams.append({"id": e["id"], "title": e["title"],
                      "exam_date": e["exam_date"], "left": left})

    return render_template(
        "index.html",
        subjects=subjects,
        sessions=sessions,
        per_subject=per_subject,
        week_total=week_total,
        all_total=all_total,
        streak=streak,
        today=date.today().isoformat(),
        last7=last7,
        heat=heat,
        exams=exams,
        tasks=[dict(r) for r in db.execute(
            "SELECT * FROM tasks ORDER BY done, id DESC").fetchall()],
    )


@app.route("/subject/add", methods=["POST"])
def add_subject():
    name = request.form.get("name", "").strip()
    if name:
        db = get_db()
        db.execute("INSERT OR IGNORE INTO subjects (name) VALUES (?)", (name,))
        db.commit()
    return redirect(url_for("index"))


@app.route("/session/add", methods=["POST"])
def add_session():
    subject_id = request.form.get("subject_id")
    topic = request.form.get("topic", "").strip()
    minutes = request.form.get("minutes", type=int)
    study_date = request.form.get("study_date") or date.today().isoformat()

    if subject_id and minutes and minutes > 0:
        db = get_db()
        db.execute(
            "INSERT INTO sessions (subject_id, topic, minutes, study_date) "
            "VALUES (?, ?, ?, ?)",
            (subject_id, topic, minutes, study_date),
        )
        db.commit()
    return redirect(url_for("index"))


@app.route("/session/delete/<int:session_id>", methods=["POST"])
def delete_session(session_id):
    db = get_db()
    db.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
    db.commit()
    return redirect(url_for("index"))


@app.route("/api/log", methods=["POST"])
def api_log():
    """Called by the focus timer (JavaScript) to save a finished session."""
    data = request.get_json(force=True)
    subject_id = data.get("subject_id")
    minutes = int(data.get("minutes", 0))
    topic = (data.get("topic") or "Focus session").strip()
    if not subject_id or minutes <= 0:
        return jsonify(ok=False), 400
    db = get_db()
    db.execute(
        "INSERT INTO sessions (subject_id, topic, minutes, study_date) VALUES (?, ?, ?, ?)",
        (subject_id, topic, minutes, date.today().isoformat()),
    )
    db.commit()
    return jsonify(ok=True)


@app.route("/exam/add", methods=["POST"])
def add_exam():
    title = request.form.get("title", "").strip()
    exam_date = request.form.get("exam_date")
    if title and exam_date:
        db = get_db()
        db.execute("INSERT INTO exams (title, exam_date) VALUES (?, ?)", (title, exam_date))
        db.commit()
    return redirect(url_for("index"))


@app.route("/exam/delete/<int:exam_id>", methods=["POST"])
def delete_exam(exam_id):
    db = get_db()
    db.execute("DELETE FROM exams WHERE id = ?", (exam_id,))
    db.commit()
    return redirect(url_for("index"))


@app.route("/task/add", methods=["POST"])
def add_task():
    title = request.form.get("title", "").strip()
    due = request.form.get("due_date") or None
    if title:
        db = get_db()
        db.execute("INSERT INTO tasks (title, due_date) VALUES (?, ?)", (title, due))
        db.commit()
    return redirect(url_for("index"))


@app.route("/task/toggle/<int:task_id>", methods=["POST"])
def toggle_task(task_id):
    db = get_db()
    db.execute("UPDATE tasks SET done = 1 - done WHERE id = ?", (task_id,))
    db.commit()
    return jsonify(ok=True)


@app.route("/task/delete/<int:task_id>", methods=["POST"])
def delete_task(task_id):
    db = get_db()
    db.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
    db.commit()
    return jsonify(ok=True)


@app.route("/session/edit/<int:session_id>", methods=["POST"])
def edit_session(session_id):
    f = request.form
    minutes = f.get("minutes", type=int)
    if f.get("subject_id") and minutes and minutes > 0:
        db = get_db()
        db.execute(
            "UPDATE sessions SET subject_id=?, topic=?, minutes=?, study_date=? WHERE id=?",
            (f["subject_id"], f.get("topic", "").strip(), minutes,
             f.get("study_date") or date.today().isoformat(), session_id),
        )
        db.commit()
    return redirect(url_for("index"))


@app.route("/subject/edit/<int:subject_id>", methods=["POST"])
def edit_subject(subject_id):
    name = request.form.get("name", "").strip()
    if name:
        db = get_db()
        try:
            db.execute("UPDATE subjects SET name=? WHERE id=?", (name, subject_id))
            db.commit()
        except sqlite3.IntegrityError:
            pass  # a subject with this name already exists
    return redirect(url_for("index"))


@app.route("/subject/delete/<int:subject_id>", methods=["POST"])
def delete_subject(subject_id):
    db = get_db()
    db.execute("DELETE FROM sessions WHERE subject_id = ?", (subject_id,))
    db.execute("DELETE FROM subjects WHERE id = ?", (subject_id,))
    db.commit()
    return redirect(url_for("index"))


@app.route("/exam/edit/<int:exam_id>", methods=["POST"])
def edit_exam(exam_id):
    title = request.form.get("title", "").strip()
    exam_date = request.form.get("exam_date")
    if title and exam_date:
        db = get_db()
        db.execute("UPDATE exams SET title=?, exam_date=? WHERE id=?", (title, exam_date, exam_id))
        db.commit()
    return redirect(url_for("index"))


@app.route("/task/edit/<int:task_id>", methods=["POST"])
def edit_task(task_id):
    title = request.form.get("title", "").strip()
    due = request.form.get("due_date") or None
    if title:
        db = get_db()
        db.execute("UPDATE tasks SET title=?, due_date=? WHERE id=?", (title, due, task_id))
        db.commit()
    return redirect(url_for("index"))


@app.route("/reset", methods=["POST"])
def reset_all():
    """Deletes ALL data. The browser asks the user to type RESET first."""
    if request.form.get("confirm") == "RESET":
        db = get_db()
        for table in ("sessions", "tasks", "exams", "subjects"):
            db.execute(f"DELETE FROM {table}")
        db.execute("DELETE FROM sqlite_sequence")
        db.commit()
    return redirect(url_for("index"))


@app.route("/health")
def health():
    """Shows where the data is stored, so you can check that it survives restarts."""
    db = get_db()
    exists = os.path.exists(DB_PATH)
    return jsonify(
        db_path=DB_PATH,
        db_file_exists=exists,
        db_size_bytes=os.path.getsize(DB_PATH) if exists else 0,
        on_azure=bool(os.environ.get("WEBSITE_SITE_NAME")),
        subjects=db.execute("SELECT COUNT(*) FROM subjects").fetchone()[0],
        sessions=db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0],
    )


@app.route("/export")
def export_csv():
    rows = get_db().execute(
        """
        SELECT s.study_date, sub.name, s.topic, s.minutes
        FROM sessions s JOIN subjects sub ON sub.id = s.subject_id
        ORDER BY s.study_date
        """
    ).fetchall()
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["Date", "Subject", "Topic", "Minutes"])
    for r in rows:
        w.writerow(list(r))
    return Response(
        out.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=study_history.csv"},
    )


init_db()

if __name__ == "__main__":
    app.run(debug=True)
