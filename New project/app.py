from __future__ import annotations

import os
import sqlite3
import tempfile
from datetime import datetime
from functools import wraps
from pathlib import Path
from typing import Any, Callable

from flask import (
    Flask,
    Response,
    current_app,
    flash,
    g,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from werkzeug.security import check_password_hash, generate_password_hash

BASE_DIR = Path(__file__).resolve().parent


def resolve_db_path() -> Path:
    configured = os.getenv("MAINTENANCE_DB_PATH")
    if configured:
        db_path = Path(configured).expanduser().resolve()
        db_path.parent.mkdir(parents=True, exist_ok=True)
        return db_path

    render_disk = Path("/var/data")
    if render_disk.exists():
        return (render_disk / "maintenance.db").resolve()

    railway_volume = os.getenv("RAILWAY_VOLUME_MOUNT_PATH")
    if railway_volume:
        db_path = Path(railway_volume).expanduser().resolve() / "maintenance.db"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        return db_path

    # En local (carpetas sincronizadas como OneDrive), SQLite puede fallar por locking.
    # Usamos la carpeta temporal del sistema como ruta segura por defecto.
    temp_base = Path(tempfile.gettempdir()) / "maintenance_ot"
    temp_base.mkdir(parents=True, exist_ok=True)
    return (temp_base / "maintenance.db").resolve()


DB_PATH = resolve_db_path()

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    full_name TEXT NOT NULL,
    role TEXT NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_login_at TEXT
);

CREATE TABLE IF NOT EXISTS assets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    asset_type TEXT NOT NULL,
    location TEXT,
    criticality TEXT,
    temp_threshold REAL,
    amp_threshold REAL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS work_orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ot_number TEXT NOT NULL UNIQUE,
    maintenance_type TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    asset_id INTEGER NOT NULL,
    requested_by TEXT,
    assigned_to TEXT,
    opened_at TEXT NOT NULL,
    due_date TEXT,
    closed_at TEXT,
    description TEXT,
    actions_taken TEXT,
    FOREIGN KEY (asset_id) REFERENCES assets (id)
);

CREATE TABLE IF NOT EXISTS measurements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    asset_id INTEGER NOT NULL,
    work_order_id INTEGER,
    measurement_type TEXT NOT NULL,
    value REAL NOT NULL,
    unit TEXT NOT NULL,
    measured_at TEXT NOT NULL,
    note TEXT,
    FOREIGN KEY (asset_id) REFERENCES assets (id),
    FOREIGN KEY (work_order_id) REFERENCES work_orders (id)
);

CREATE TABLE IF NOT EXISTS failures (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    asset_id INTEGER NOT NULL,
    work_order_id INTEGER,
    failure_mode TEXT NOT NULL,
    severity TEXT NOT NULL,
    detected_at TEXT NOT NULL,
    description TEXT NOT NULL,
    root_cause TEXT,
    corrective_action TEXT,
    downtime_minutes INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (asset_id) REFERENCES assets (id),
    FOREIGN KEY (work_order_id) REFERENCES work_orders (id)
);

CREATE INDEX IF NOT EXISTS idx_measurements_asset ON measurements(asset_id);
CREATE INDEX IF NOT EXISTS idx_measurements_work_order ON measurements(work_order_id);
CREATE INDEX IF NOT EXISTS idx_failures_asset ON failures(asset_id);
CREATE INDEX IF NOT EXISTS idx_failures_work_order ON failures(work_order_id);
CREATE INDEX IF NOT EXISTS idx_work_orders_asset ON work_orders(asset_id);
CREATE INDEX IF NOT EXISTS idx_users_username ON users(username);
"""

ASSET_TYPES = ["motor", "tablero", "bomba", "compresor", "generador", "otro"]
MAINTENANCE_TYPES = ["predictiva", "preventiva", "correctiva"]
WORK_ORDER_STATUSES = ["open", "in_progress", "closed"]
MEASUREMENT_TYPES = ["temperature", "amperage", "vibration", "other"]
SEVERITIES = ["low", "medium", "high", "critical"]
USER_ROLES = ["operator", "planner", "supervisor"]
ROLE_LABELS = {
    "operator": "Operador",
    "planner": "Planificador",
    "supervisor": "Supervisor",
}


def login_required(view: Callable[..., Any]) -> Callable[..., Any]:
    @wraps(view)
    def wrapped_view(*args: Any, **kwargs: Any) -> Any:
        if g.get("user") is None:
            flash("Debes iniciar sesion para continuar.", "error")
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)

    return wrapped_view


def roles_required(*allowed_roles: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    def decorator(view: Callable[..., Any]) -> Callable[..., Any]:
        @wraps(view)
        def wrapped_view(*args: Any, **kwargs: Any) -> Any:
            user = g.get("user")
            if user is None:
                flash("Debes iniciar sesion para continuar.", "error")
                return redirect(url_for("login", next=request.path))
            if user["role"] not in allowed_roles:
                flash("No tienes permisos para esta accion.", "error")
                return redirect(url_for("dashboard"))
            return view(*args, **kwargs)

        return wrapped_view

    return decorator


def create_app() -> Flask:
    app = Flask(__name__)
    app.config["DATABASE"] = str(DB_PATH)
    app.config["SECRET_KEY"] = os.getenv("MAINTENANCE_SECRET_KEY", "maintenance-dev-secret-key")

    with app.app_context():
        init_db()
        ensure_default_supervisor()

    @app.teardown_appcontext
    def close_db(_: Any) -> None:
        db = g.pop("db", None)
        if db is not None:
            db.close()

    @app.before_request
    def load_current_user() -> None:
        if request.endpoint == "static":
            g.user = None
            return

        user_id = session.get("user_id")
        if user_id is None:
            g.user = None
            return

        db = get_db()
        user = db.execute(
            """
            SELECT id, username, full_name, role, is_active, created_at, last_login_at
            FROM users
            WHERE id = ?
            """,
            (user_id,),
        ).fetchone()

        if user is None or user["is_active"] != 1:
            session.clear()
            g.user = None
            return

        g.user = user

    @app.context_processor
    def inject_template_context() -> dict[str, Any]:
        user = g.get("user")
        is_planner_or_supervisor = bool(user and user["role"] in {"planner", "supervisor"})
        is_supervisor = bool(user and user["role"] == "supervisor")
        return {
            "current_user": user,
            "is_planner_or_supervisor": is_planner_or_supervisor,
            "is_supervisor": is_supervisor,
            "role_labels": ROLE_LABELS,
        }

    @app.get("/manifest.webmanifest")
    def manifest() -> Response:
        return current_app.send_static_file("manifest.webmanifest")

    @app.get("/service-worker.js")
    def service_worker() -> Response:
        response = current_app.send_static_file("service-worker.js")
        response.headers["Cache-Control"] = "no-cache"
        return response

    @app.get("/health")
    def health() -> tuple[dict[str, str], int]:
        return {"status": "ok"}, 200

    @app.route("/login", methods=["GET", "POST"])
    def login() -> str | Response:
        if g.get("user") is not None:
            return redirect(url_for("dashboard"))

        next_url = request.args.get("next", "")
        if request.method == "POST":
            username = request.form.get("username", "").strip().lower()
            password = request.form.get("password", "")
            next_url = request.form.get("next", "")

            db = get_db()
            user = db.execute(
                """
                SELECT id, username, full_name, role, password_hash, is_active
                FROM users
                WHERE username = ?
                """,
                (username,),
            ).fetchone()

            if user is None or user["is_active"] != 1 or not check_password_hash(user["password_hash"], password):
                flash("Usuario o clave invalida.", "error")
                return render_template("login.html", next_url=next_url)

            session.clear()
            session["user_id"] = user["id"]

            db.execute(
                "UPDATE users SET last_login_at = ? WHERE id = ?",
                (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), user["id"]),
            )
            db.commit()

            if is_safe_next_url(next_url):
                return redirect(next_url)
            return redirect(url_for("dashboard"))

        return render_template("login.html", next_url=next_url)

    @app.post("/logout")
    @login_required
    def logout() -> Response:
        session.clear()
        flash("Sesion cerrada.", "success")
        return redirect(url_for("login"))

    @app.route("/profile", methods=["GET", "POST"])
    @login_required
    def profile() -> str | Response:
        if request.method == "POST":
            current_password = request.form.get("current_password", "")
            new_password = request.form.get("new_password", "")
            confirm_password = request.form.get("confirm_password", "")

            if len(new_password) < 8:
                flash("La nueva clave debe tener al menos 8 caracteres.", "error")
                return redirect(url_for("profile"))

            if new_password != confirm_password:
                flash("La confirmacion no coincide.", "error")
                return redirect(url_for("profile"))

            db = get_db()
            user = db.execute(
                "SELECT id, password_hash FROM users WHERE id = ?",
                (g.user["id"],),
            ).fetchone()

            if user is None or not check_password_hash(user["password_hash"], current_password):
                flash("La clave actual no es correcta.", "error")
                return redirect(url_for("profile"))

            db.execute(
                "UPDATE users SET password_hash = ? WHERE id = ?",
                (generate_password_hash(new_password), user["id"]),
            )
            db.commit()
            flash("Clave actualizada correctamente.", "success")
            return redirect(url_for("profile"))

        return render_template("profile.html")

    @app.route("/users", methods=["GET", "POST"])
    @roles_required("supervisor")
    def users_page() -> str | Response:
        db = get_db()

        if request.method == "POST":
            username = request.form.get("username", "").strip().lower()
            full_name = request.form.get("full_name", "").strip()
            role = request.form.get("role", "").strip()
            password = request.form.get("password", "")

            if not username or not full_name or role not in USER_ROLES or len(password) < 8:
                flash("Completa usuario, nombre, rol valido y clave de 8+ caracteres.", "error")
                return redirect(url_for("users_page"))

            try:
                db.execute(
                    """
                    INSERT INTO users (username, password_hash, full_name, role)
                    VALUES (?, ?, ?, ?)
                    """,
                    (username, generate_password_hash(password), full_name, role),
                )
                db.commit()
                flash(f"Usuario {username} creado.", "success")
            except sqlite3.IntegrityError:
                flash(f"El usuario {username} ya existe.", "error")

            return redirect(url_for("users_page"))

        users_data = db.execute(
            """
            SELECT id, username, full_name, role, is_active, created_at, last_login_at
            FROM users
            ORDER BY created_at DESC
            """
        ).fetchall()

        return render_template("users.html", users=users_data, roles=USER_ROLES)

    @app.post("/users/<int:user_id>/toggle")
    @roles_required("supervisor")
    def toggle_user(user_id: int) -> Response:
        if user_id == g.user["id"]:
            flash("No puedes desactivar tu propio usuario.", "error")
            return redirect(url_for("users_page"))

        db = get_db()
        user = db.execute("SELECT id, is_active, username FROM users WHERE id = ?", (user_id,)).fetchone()
        if user is None:
            flash("Usuario no encontrado.", "error")
            return redirect(url_for("users_page"))

        new_state = 0 if user["is_active"] == 1 else 1
        db.execute("UPDATE users SET is_active = ? WHERE id = ?", (new_state, user_id))
        db.commit()

        state_label = "activado" if new_state == 1 else "desactivado"
        flash(f"Usuario {user['username']} {state_label}.", "success")
        return redirect(url_for("users_page"))

    @app.route("/")
    @login_required
    def dashboard() -> str:
        db = get_db()
        metrics = db.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM work_orders WHERE status <> 'closed') AS open_orders,
                (SELECT COUNT(*) FROM work_orders) AS total_orders,
                (SELECT COUNT(*) FROM failures) AS total_failures,
                (SELECT COUNT(*) FROM assets) AS total_assets,
                (SELECT COUNT(*) FROM failures WHERE severity = 'critical') AS critical_failures
            """
        ).fetchone()

        mttr_row = db.execute(
            """
            SELECT AVG(downtime_minutes) / 60.0 AS mttr_hours
            FROM failures
            WHERE downtime_minutes > 0
            """
        ).fetchone()

        mtbf_row = db.execute(
            """
            SELECT AVG(hours_between) AS mtbf_hours
            FROM (
                SELECT
                    (
                        julianday(REPLACE(detected_at, 'T', ' '))
                        - julianday(REPLACE(prev_detected_at, 'T', ' '))
                    ) * 24.0 AS hours_between
                FROM (
                    SELECT
                        asset_id,
                        detected_at,
                        LAG(detected_at) OVER (
                            PARTITION BY asset_id
                            ORDER BY julianday(REPLACE(detected_at, 'T', ' '))
                        ) AS prev_detected_at
                    FROM failures
                ) ordered_failures
            ) intervals
            WHERE hours_between IS NOT NULL AND hours_between > 0
            """
        ).fetchone()

        preventive_row = db.execute(
            """
            SELECT
                SUM(CASE WHEN maintenance_type = 'preventiva' AND due_date IS NOT NULL THEN 1 ELSE 0 END) AS total,
                SUM(
                    CASE
                        WHEN maintenance_type = 'preventiva'
                             AND due_date IS NOT NULL
                             AND status = 'closed'
                             AND closed_at IS NOT NULL
                             AND date(closed_at) <= date(due_date)
                        THEN 1
                        ELSE 0
                    END
                ) AS on_time
            FROM work_orders
            """
        ).fetchone()

        preventive_total = preventive_row["total"] or 0
        preventive_on_time = preventive_row["on_time"] or 0
        preventive_compliance = (preventive_on_time / preventive_total * 100.0) if preventive_total > 0 else None

        kpis = {
            "mttr_hours": format_hours(mttr_row["mttr_hours"] if mttr_row else None),
            "mtbf_hours": format_hours(mtbf_row["mtbf_hours"] if mtbf_row else None),
            "preventive_compliance": format_percent(preventive_compliance),
        }

        latest_alerts = db.execute(
            """
            SELECT
                m.id,
                m.measurement_type,
                m.value,
                m.unit,
                m.measured_at,
                a.code AS asset_code,
                a.name AS asset_name,
                a.temp_threshold,
                a.amp_threshold
            FROM measurements m
            JOIN assets a ON a.id = m.asset_id
            WHERE
                (m.measurement_type = 'temperature' AND a.temp_threshold IS NOT NULL AND m.value > a.temp_threshold)
                OR
                (m.measurement_type = 'amperage' AND a.amp_threshold IS NOT NULL AND m.value > a.amp_threshold)
            ORDER BY datetime(REPLACE(m.measured_at, 'T', ' ')) DESC
            LIMIT 10
            """
        ).fetchall()

        latest_failures = db.execute(
            """
            SELECT
                f.id,
                f.failure_mode,
                f.severity,
                f.detected_at,
                a.code AS asset_code,
                a.name AS asset_name,
                wo.ot_number
            FROM failures f
            JOIN assets a ON a.id = f.asset_id
            LEFT JOIN work_orders wo ON wo.id = f.work_order_id
            ORDER BY datetime(REPLACE(f.detected_at, 'T', ' ')) DESC
            LIMIT 10
            """
        ).fetchall()

        open_orders = db.execute(
            """
            SELECT
                wo.id,
                wo.ot_number,
                wo.maintenance_type,
                wo.status,
                wo.opened_at,
                wo.due_date,
                a.code AS asset_code,
                a.name AS asset_name
            FROM work_orders wo
            JOIN assets a ON a.id = wo.asset_id
            WHERE wo.status <> 'closed'
            ORDER BY datetime(wo.opened_at) DESC
            LIMIT 10
            """
        ).fetchall()

        return render_template(
            "dashboard.html",
            metrics=metrics,
            kpis=kpis,
            latest_alerts=latest_alerts,
            latest_failures=latest_failures,
            open_orders=open_orders,
        )

    @app.route("/assets", methods=["GET", "POST"])
    @login_required
    def assets() -> str:
        db = get_db()
        if request.method == "POST":
            if not user_has_roles("planner", "supervisor"):
                flash("Solo planificador o supervisor puede crear activos.", "error")
                return redirect(url_for("assets"))

            code = request.form.get("code", "").strip().upper()
            name = request.form.get("name", "").strip()
            asset_type = request.form.get("asset_type", "").strip()
            location = request.form.get("location", "").strip()
            criticality = request.form.get("criticality", "").strip()
            temp_threshold = parse_optional_float(request.form.get("temp_threshold"))
            amp_threshold = parse_optional_float(request.form.get("amp_threshold"))

            if not code or not name or asset_type not in ASSET_TYPES:
                flash("Completa código, nombre y tipo de activo válido.", "error")
                return redirect(url_for("assets"))

            try:
                db.execute(
                    """
                    INSERT INTO assets (code, name, asset_type, location, criticality, temp_threshold, amp_threshold)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (code, name, asset_type, location, criticality, temp_threshold, amp_threshold),
                )
                db.commit()
                flash(f"Activo {code} registrado.", "success")
            except sqlite3.IntegrityError:
                flash(f"Ya existe un activo con código {code}.", "error")

            return redirect(url_for("assets"))

        rows = db.execute(
            """
            SELECT id, code, name, asset_type, location, criticality, temp_threshold, amp_threshold, created_at
            FROM assets
            ORDER BY datetime(created_at) DESC
            """
        ).fetchall()
        return render_template("assets.html", assets=rows, asset_types=ASSET_TYPES)

    @app.route("/work-orders", methods=["GET", "POST"])
    @login_required
    def work_orders() -> str:
        db = get_db()
        assets_data = db.execute(
            "SELECT id, code, name FROM assets ORDER BY code"
        ).fetchall()

        if request.method == "POST":
            if not user_has_roles("planner", "supervisor"):
                flash("Solo planificador o supervisor puede crear OT.", "error")
                return redirect(url_for("work_orders"))

            if not assets_data:
                flash("Debes registrar al menos un activo antes de crear OT.", "error")
                return redirect(url_for("assets"))

            ot_number = request.form.get("ot_number", "").strip().upper()
            maintenance_type = request.form.get("maintenance_type", "").strip()
            status = request.form.get("status", "open").strip()
            asset_id = request.form.get("asset_id", "").strip()
            requested_by = request.form.get("requested_by", "").strip()
            assigned_to = request.form.get("assigned_to", "").strip()
            opened_at = request.form.get("opened_at", "").strip()
            due_date = request.form.get("due_date", "").strip()
            description = request.form.get("description", "").strip()

            if (
                not ot_number
                or maintenance_type not in MAINTENANCE_TYPES
                or status not in WORK_ORDER_STATUSES
                or not asset_id.isdigit()
                or not opened_at
            ):
                flash("Revisa los campos obligatorios de la OT.", "error")
                return redirect(url_for("work_orders"))

            try:
                db.execute(
                    """
                    INSERT INTO work_orders
                    (ot_number, maintenance_type, status, asset_id, requested_by, assigned_to, opened_at, due_date, description)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        ot_number,
                        maintenance_type,
                        status,
                        int(asset_id),
                        requested_by,
                        assigned_to,
                        opened_at,
                        due_date or None,
                        description,
                    ),
                )
                db.commit()
                flash(f"OT {ot_number} registrada.", "success")
            except sqlite3.IntegrityError:
                flash(f"La OT {ot_number} ya existe.", "error")

            return redirect(url_for("work_orders"))

        rows = db.execute(
            """
            SELECT
                wo.id,
                wo.ot_number,
                wo.maintenance_type,
                wo.status,
                wo.opened_at,
                wo.due_date,
                wo.closed_at,
                wo.requested_by,
                wo.assigned_to,
                a.code AS asset_code,
                a.name AS asset_name
            FROM work_orders wo
            JOIN assets a ON a.id = wo.asset_id
            ORDER BY datetime(wo.opened_at) DESC
            """
        ).fetchall()
        return render_template(
            "work_orders.html",
            work_orders=rows,
            assets=assets_data,
            maintenance_types=MAINTENANCE_TYPES,
            statuses=WORK_ORDER_STATUSES,
            today=datetime.now().strftime("%Y-%m-%d"),
        )

    @app.post("/work-orders/<int:work_order_id>/close")
    @roles_required("planner", "supervisor")
    def close_work_order(work_order_id: int) -> Any:
        db = get_db()
        row = db.execute(
            "SELECT id, ot_number FROM work_orders WHERE id = ?",
            (work_order_id,),
        ).fetchone()
        if row is None:
            flash("No se encontró la OT.", "error")
            return redirect(url_for("work_orders"))

        db.execute(
            "UPDATE work_orders SET status = 'closed', closed_at = ? WHERE id = ?",
            (datetime.now().strftime("%Y-%m-%d"), work_order_id),
        )
        db.commit()
        flash(f"OT {row['ot_number']} cerrada.", "success")
        return redirect(url_for("work_orders"))

    @app.route("/measurements", methods=["GET", "POST"])
    @login_required
    def measurements() -> str:
        db = get_db()
        assets_data = db.execute(
            "SELECT id, code, name FROM assets ORDER BY code"
        ).fetchall()
        work_orders_data = db.execute(
            "SELECT id, ot_number FROM work_orders ORDER BY datetime(opened_at) DESC"
        ).fetchall()

        if request.method == "POST":
            if not assets_data:
                flash("Debes registrar un activo antes de cargar mediciones.", "error")
                return redirect(url_for("assets"))

            asset_id = request.form.get("asset_id", "").strip()
            work_order_id = request.form.get("work_order_id", "").strip()
            measurement_type = request.form.get("measurement_type", "").strip()
            value = request.form.get("value", "").strip()
            unit = request.form.get("unit", "").strip()
            measured_at = request.form.get("measured_at", "").strip()
            note = request.form.get("note", "").strip()

            if (
                not asset_id.isdigit()
                or measurement_type not in MEASUREMENT_TYPES
                or not is_float(value)
                or not unit
                or not measured_at
            ):
                flash("Revisa los campos obligatorios de la medición.", "error")
                return redirect(url_for("measurements"))

            db.execute(
                """
                INSERT INTO measurements (asset_id, work_order_id, measurement_type, value, unit, measured_at, note)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    int(asset_id),
                    int(work_order_id) if work_order_id.isdigit() else None,
                    measurement_type,
                    float(value),
                    unit,
                    measured_at,
                    note,
                ),
            )
            db.commit()
            flash("Medición registrada.", "success")
            return redirect(url_for("measurements"))

        rows = db.execute(
            """
            SELECT
                m.id,
                m.measurement_type,
                m.value,
                m.unit,
                m.measured_at,
                m.note,
                a.code AS asset_code,
                a.name AS asset_name,
                wo.ot_number,
                CASE
                    WHEN m.measurement_type = 'temperature' AND a.temp_threshold IS NOT NULL AND m.value > a.temp_threshold THEN 1
                    WHEN m.measurement_type = 'amperage' AND a.amp_threshold IS NOT NULL AND m.value > a.amp_threshold THEN 1
                    ELSE 0
                END AS is_alert
            FROM measurements m
            JOIN assets a ON a.id = m.asset_id
            LEFT JOIN work_orders wo ON wo.id = m.work_order_id
            ORDER BY datetime(REPLACE(m.measured_at, 'T', ' ')) DESC
            LIMIT 200
            """
        ).fetchall()

        return render_template(
            "measurements.html",
            measurements=rows,
            assets=assets_data,
            work_orders=work_orders_data,
            measurement_types=MEASUREMENT_TYPES,
            now=datetime.now().strftime("%Y-%m-%dT%H:%M"),
        )

    @app.route("/failures", methods=["GET", "POST"])
    @login_required
    def failures() -> str:
        db = get_db()
        assets_data = db.execute(
            "SELECT id, code, name FROM assets ORDER BY code"
        ).fetchall()
        work_orders_data = db.execute(
            "SELECT id, ot_number FROM work_orders ORDER BY datetime(opened_at) DESC"
        ).fetchall()

        if request.method == "POST":
            if not assets_data:
                flash("Debes registrar un activo antes de registrar fallas.", "error")
                return redirect(url_for("assets"))

            asset_id = request.form.get("asset_id", "").strip()
            work_order_id = request.form.get("work_order_id", "").strip()
            failure_mode = request.form.get("failure_mode", "").strip()
            severity = request.form.get("severity", "").strip()
            detected_at = request.form.get("detected_at", "").strip()
            description = request.form.get("description", "").strip()
            root_cause = request.form.get("root_cause", "").strip()
            corrective_action = request.form.get("corrective_action", "").strip()
            downtime_minutes = request.form.get("downtime_minutes", "").strip()

            if (
                not asset_id.isdigit()
                or not failure_mode
                or severity not in SEVERITIES
                or not detected_at
                or not description
                or not is_int(downtime_minutes or "0")
            ):
                flash("Revisa los campos obligatorios de la falla.", "error")
                return redirect(url_for("failures"))

            db.execute(
                """
                INSERT INTO failures
                (asset_id, work_order_id, failure_mode, severity, detected_at, description, root_cause, corrective_action, downtime_minutes)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    int(asset_id),
                    int(work_order_id) if work_order_id.isdigit() else None,
                    failure_mode,
                    severity,
                    detected_at,
                    description,
                    root_cause,
                    corrective_action,
                    int(downtime_minutes or 0),
                ),
            )
            db.commit()
            flash("Falla registrada.", "success")
            return redirect(url_for("failures"))

        rows = db.execute(
            """
            SELECT
                f.id,
                f.failure_mode,
                f.severity,
                f.detected_at,
                f.description,
                f.root_cause,
                f.corrective_action,
                f.downtime_minutes,
                a.code AS asset_code,
                a.name AS asset_name,
                wo.ot_number
            FROM failures f
            JOIN assets a ON a.id = f.asset_id
            LEFT JOIN work_orders wo ON wo.id = f.work_order_id
            ORDER BY datetime(REPLACE(f.detected_at, 'T', ' ')) DESC
            LIMIT 200
            """
        ).fetchall()

        return render_template(
            "failures.html",
            failures=rows,
            assets=assets_data,
            work_orders=work_orders_data,
            severities=SEVERITIES,
            now=datetime.now().strftime("%Y-%m-%dT%H:%M"),
        )

    return app


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        db = sqlite3.connect(current_app.config["DATABASE"], timeout=30)
        db.row_factory = sqlite3.Row
        g.db = db
    return g.db


def init_db() -> None:
    db = get_db()
    db.executescript(SCHEMA_SQL)
    db.commit()


def ensure_default_supervisor() -> None:
    db = get_db()
    default_user = os.getenv("MAINTENANCE_DEFAULT_USER", "admin").strip().lower()
    default_password = os.getenv("MAINTENANCE_DEFAULT_PASSWORD", "admin12345")
    default_name = os.getenv("MAINTENANCE_DEFAULT_NAME", "Administrador")

    existing = db.execute("SELECT id FROM users WHERE username = ?", (default_user,)).fetchone()
    if existing is not None:
        return

    db.execute(
        """
        INSERT OR IGNORE INTO users (username, password_hash, full_name, role)
        VALUES (?, ?, ?, 'supervisor')
        """,
        (default_user, generate_password_hash(default_password), default_name),
    )
    db.commit()


def user_has_roles(*roles: str) -> bool:
    user = g.get("user")
    return bool(user and user["role"] in roles)


def is_safe_next_url(next_url: str) -> bool:
    return bool(next_url) and next_url.startswith("/") and not next_url.startswith("//")


def format_hours(value: float | None) -> str:
    if value is None:
        return "-"
    if value < 0:
        return "-"
    return f"{value:.2f} h"


def format_percent(value: float | None) -> str:
    if value is None:
        return "-"
    return f"{value:.1f}%"


def parse_optional_float(value: str | None) -> float | None:
    if value is None:
        return None
    clean = value.strip()
    if not clean:
        return None
    if is_float(clean):
        return float(clean)
    return None


def is_float(value: str) -> bool:
    try:
        float(value)
        return True
    except ValueError:
        return False


def is_int(value: str) -> bool:
    try:
        int(value)
        return True
    except ValueError:
        return False


app = create_app()


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
