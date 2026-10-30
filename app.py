import os
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory


ROOT = Path(__file__).resolve().parent.parent
FRONTEND = ROOT / "frontend"
FIELDS = ("student_id", "name", "email", "course", "year", "status")
STATUSES = {"Active", "Inactive"}
EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def create_app(test_config=None):
    app = Flask(__name__, static_folder=str(FRONTEND), static_url_path="")
    app.config.from_mapping(
        DATABASE=os.environ.get(
            "STUDENT_DB_PATH", str(Path(__file__).resolve().parent / "students.db")
        )
    )
    if test_config:
        app.config.update(test_config)

    initialize_database(app.config["DATABASE"])

    @contextmanager
    def connect():
        connection = sqlite3.connect(app.config["DATABASE"])
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def student_dict(row):
        return dict(row)

    def error(message, status):
        return jsonify({"error": message}), status

    def validate_student(data, current=None):
        if not isinstance(data, dict):
            return None, "A JSON object is required."

        unknown = set(data) - set(FIELDS)
        if unknown:
            return None, f"Unknown field(s): {', '.join(sorted(unknown))}."

        values = dict(current or {})
        values.update(data)
        required = ("student_id", "name", "email", "course", "year", "status")

        for field in required:
            value = values.get(field)
            if value is None or (isinstance(value, str) and not value.strip()):
                return None, f"{field.replace('_', ' ').capitalize()} is required."
            if isinstance(value, str):
                values[field] = value.strip()

        for field, limit in (("student_id", 40), ("name", 120), ("email", 254), ("course", 100)):
            if len(values[field]) > limit:
                return None, f"{field.replace('_', ' ').capitalize()} must be {limit} characters or fewer."

        if not EMAIL_PATTERN.fullmatch(values["email"]):
            return None, "Enter a valid email address."

        if isinstance(values["year"], bool):
            return None, "Year must be a whole number between 1 and 8."
        try:
            year = int(values["year"])
        except (TypeError, ValueError):
            return None, "Year must be a whole number between 1 and 8."
        if str(values["year"]).strip() != str(year) or not 1 <= year <= 8:
            return None, "Year must be a whole number between 1 and 8."
        values["year"] = year

        if values["status"] not in STATUSES:
            return None, "Status must be Active or Inactive."

        return {field: values[field] for field in FIELDS}, None

    @app.get("/")
    def home():
        return send_from_directory(FRONTEND, "index.html")

    @app.get("/api/health")
    def health():
        return jsonify({"status": "ok"})

    @app.get("/api/students")
    def list_students():
        search = request.args.get("search", "").strip()
        status = request.args.get("status", "").strip()
        try:
            page = max(1, int(request.args.get("page", "1")))
            per_page = min(100, max(1, int(request.args.get("per_page", "100"))))
        except ValueError:
            return error("page and per_page must be whole numbers.", 400)
        if status and status not in STATUSES:
            return error("Status must be Active or Inactive.", 400)

        clauses = []
        parameters = []
        if search:
            clauses.append(
                "(student_id LIKE ? OR name LIKE ? OR email LIKE ? OR course LIKE ?)"
            )
            pattern = f"%{search}%"
            parameters.extend([pattern] * 4)
        if status:
            clauses.append("status = ?")
            parameters.append(status)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""

        with connect() as connection:
            total = connection.execute(
                f"SELECT COUNT(*) FROM students{where}", parameters
            ).fetchone()[0]
            rows = connection.execute(
                f"SELECT * FROM students{where} ORDER BY name COLLATE NOCASE, id LIMIT ? OFFSET ?",
                [*parameters, per_page, (page - 1) * per_page],
            ).fetchall()
        return jsonify(
            {
                "students": [student_dict(row) for row in rows],
                "total": total,
                "page": page,
                "per_page": per_page,
            }
        )

    @app.get("/api/students/summary")
    def student_summary():
        with connect() as connection:
            total, active, inactive = connection.execute(
                "SELECT COUNT(*), "
                "COALESCE(SUM(status = 'Active'), 0), "
                "COALESCE(SUM(status = 'Inactive'), 0) FROM students"
            ).fetchone()
            courses = connection.execute(
                "SELECT COUNT(DISTINCT course) FROM students"
            ).fetchone()[0]
        return jsonify(
            {"total": total, "active": active, "inactive": inactive, "courses": courses}
        )

    @app.post("/api/students")
    def create_student():
        data, validation_error = validate_student(request.get_json(silent=True))
        if validation_error:
            return error(validation_error, 400)
        try:
            with connect() as connection:
                cursor = connection.execute(
                    "INSERT INTO students (student_id, name, email, course, year, status) "
                    "VALUES (:student_id, :name, :email, :course, :year, :status)",
                    data,
                )
                row = connection.execute(
                    "SELECT * FROM students WHERE id = ?", (cursor.lastrowid,)
                ).fetchone()
        except sqlite3.IntegrityError:
            return error("Student ID or email is already in use.", 409)
        return jsonify(student_dict(row)), 201

    @app.get("/api/students/<int:student_pk>")
    def get_student(student_pk):
        with connect() as connection:
            row = connection.execute(
                "SELECT * FROM students WHERE id = ?", (student_pk,)
            ).fetchone()
        if row is None:
            return error("Student not found.", 404)
        return jsonify(student_dict(row))

    @app.route("/api/students/<int:student_pk>", methods=["PUT", "PATCH"])
    def update_student(student_pk):
        incoming = request.get_json(silent=True)
        if not isinstance(incoming, dict):
            return error("A JSON object is required.", 400)

        with connect() as connection:
            existing = connection.execute(
                "SELECT * FROM students WHERE id = ?", (student_pk,)
            ).fetchone()
            if existing is None:
                return error("Student not found.", 404)
            data, validation_error = validate_student(
                incoming, current=student_dict(existing)
            )
            if validation_error:
                return error(validation_error, 400)
            try:
                connection.execute(
                    "UPDATE students SET student_id = :student_id, name = :name, "
                    "email = :email, course = :course, year = :year, status = :status, "
                    "updated_at = CURRENT_TIMESTAMP WHERE id = :id",
                    {**data, "id": student_pk},
                )
            except sqlite3.IntegrityError:
                return error("Student ID or email is already in use.", 409)
            updated = connection.execute(
                "SELECT * FROM students WHERE id = ?", (student_pk,)
            ).fetchone()
        return jsonify(student_dict(updated))

    @app.delete("/api/students/<int:student_pk>")
    def delete_student(student_pk):
        with connect() as connection:
            cursor = connection.execute(
                "DELETE FROM students WHERE id = ?", (student_pk,)
            )
        if cursor.rowcount == 0:
            return error("Student not found.", 404)
        return "", 204

    return app


def initialize_database(database_path):
    Path(database_path).parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database_path)
    try:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS students (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                student_id TEXT NOT NULL COLLATE NOCASE UNIQUE,
                name TEXT NOT NULL,
                email TEXT NOT NULL COLLATE NOCASE UNIQUE,
                course TEXT NOT NULL,
                year INTEGER NOT NULL CHECK (year BETWEEN 1 AND 8),
                status TEXT NOT NULL CHECK (status IN ('Active', 'Inactive')),
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
    finally:
        connection.close()


app = create_app()


if __name__ == "__main__":
    if os.environ.get("OPEN_BROWSER") == "1":
        from threading import Timer
        from webbrowser import open_new_tab

        Timer(1, open_new_tab, args=("http://127.0.0.1:5000",)).start()
    app.run(debug=os.environ.get("FLASK_DEBUG") == "1")
