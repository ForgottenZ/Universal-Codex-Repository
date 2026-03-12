import os
import json
import time
import uuid
import queue
import random
import threading
import sqlite3
import re
from datetime import datetime, timezone
from collections import defaultdict

from flask import (
    Flask,
    render_template,
    request,
    redirect,
    url_for,
    flash,
    session,
    jsonify,
    send_from_directory,
    abort,
    g,
    Response,
    stream_with_context,
)
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash, check_password_hash

APP_ROOT = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB_PATH = os.path.join(APP_ROOT, "data.sqlite3")
DB_PATH = os.environ.get("SPINWHEEL_DB", DEFAULT_DB_PATH)
UPLOAD_FOLDER = os.environ.get("SPINWHEEL_UPLOADS", os.path.join(APP_ROOT, "uploads"))

ALLOWED_IMAGE_EXTS = {"png", "jpg", "jpeg", "gif", "webp", "svg", "ico"}
ONLINE_TTL_SECONDS = 60
SPIN_DURATION_SECONDS = float(os.environ.get("SPINWHEEL_SPIN_DURATION", "6.0"))
MAX_SPIN_RECORDS = 200
MAX_ITEM_LOGS = 400

DEFAULT_SITE_TITLE = "大转盘"
DEFAULT_SITE_SUBTITLE = "房间独立 · 实时同步"
DEFAULT_FOOTER_TEXT = "大转盘系统 · SQLite 存储 · SSE 实时同步"
DEFAULT_JOIN_PATH = "/rooms"
DEFAULT_JOIN_PARAM = "key"

COLOR_RE = re.compile(r"^#([0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_or_create_secret_key() -> str:
    env_key = os.environ.get("SECRET_KEY")
    if env_key:
        return env_key

    path = os.path.join(APP_ROOT, ".secret_key")
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                key = f.read().strip()
                if key:
                    return key
        except Exception:
            pass

    key = uuid.uuid4().hex + uuid.uuid4().hex
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(key)
    except Exception:
        # If writing fails, still return an in-memory key.
        pass
    return key


app = Flask(__name__)
app.config["SECRET_KEY"] = load_or_create_secret_key()
app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER
app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024  # 10MB


def connect_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        g.db = connect_db()
    return g.db


@app.teardown_appcontext
def close_db(exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def _table_has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(r["name"] == column for r in rows)


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
    if _table_has_column(conn, table, column):
        return
    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def _hsl_to_hex(h: float, s: float, l: float) -> str:
    # h: 0-360, s/l: 0-100
    h = (h % 360) / 360.0
    s = max(0.0, min(1.0, s / 100.0))
    l = max(0.0, min(1.0, l / 100.0))

    def hue_to_rgb(p: float, q: float, t: float) -> float:
        if t < 0:
            t += 1
        if t > 1:
            t -= 1
        if t < 1 / 6:
            return p + (q - p) * 6 * t
        if t < 1 / 2:
            return q
        if t < 2 / 3:
            return p + (q - p) * (2 / 3 - t) * 6
        return p

    if s == 0:
        r = g = b = l
    else:
        q = l * (1 + s) if l < 0.5 else l + s - l * s
        p = 2 * l - q
        r = hue_to_rgb(p, q, h + 1 / 3)
        g = hue_to_rgb(p, q, h)
        b = hue_to_rgb(p, q, h - 1 / 3)

    return "#{:02x}{:02x}{:02x}".format(int(r * 255), int(g * 255), int(b * 255))


def normalize_hex_color(color: str | None) -> str | None:
    if not color:
        return None
    c = color.strip()
    if not c:
        return None
    if not COLOR_RE.match(c):
        return None
    if len(c) == 4:
        # #rgb -> #rrggbb
        r, g_, b = c[1], c[2], c[3]
        c = f"#{r}{r}{g_}{g_}{b}{b}"
    return c.lower()


def deterministic_pastel_color(seed: str) -> str:
    # Stable color for migrated/legacy rows
    h = 0
    for ch in seed:
        h = ((h << 5) - h) + ord(ch)
        h &= 0xFFFFFFFF
    hue = int(h % 360)
    return _hsl_to_hex(hue, 72, 86)


def random_pastel_color() -> str:
    hue = random.randint(0, 359)
    sat = random.randint(62, 78)
    lig = random.randint(82, 90)
    return _hsl_to_hex(hue, sat, lig)


def init_db() -> None:
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    os.makedirs(UPLOAD_FOLDER, exist_ok=True)
    os.makedirs(os.path.join(UPLOAD_FOLDER, "icons"), exist_ok=True)
    os.makedirs(os.path.join(UPLOAD_FOLDER, "items"), exist_ok=True)

    conn = connect_db()
    cur = conn.cursor()

    # Create tables (latest schema). For existing DBs, we also run lightweight migrations below.
    cur.executescript(
        """
        CREATE TABLE IF NOT EXISTS admin_users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS rooms (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            room_key TEXT UNIQUE NOT NULL,
            room_name TEXT NOT NULL,
            review_enabled INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            room_id INTEGER NOT NULL,
            text TEXT NOT NULL,
            image_url TEXT,
            weight REAL NOT NULL DEFAULT 1.0,
            color TEXT,
            is_deleted INTEGER NOT NULL DEFAULT 0,
            deleted_at TEXT,
            created_by_nick TEXT,
            created_by_ip TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(room_id) REFERENCES rooms(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS spins (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            room_id INTEGER NOT NULL,
            item_id INTEGER,
            item_text_snapshot TEXT,
            item_image_snapshot TEXT,
            created_by_nick TEXT,
            created_by_ip TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(room_id) REFERENCES rooms(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS room_state (
            room_id INTEGER PRIMARY KEY,
            is_spinning INTEGER NOT NULL DEFAULT 0,
            spinning_by_nick TEXT,
            spinning_by_ip TEXT,
            spinning_started_at TEXT,
            last_spin_id INTEGER,
            FOREIGN KEY(room_id) REFERENCES rooms(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS site_settings (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            site_title TEXT NOT NULL,
            site_subtitle TEXT NOT NULL,
            icon_url TEXT,
            icon_path TEXT,
            footer_text TEXT,
            join_path TEXT,
            join_param TEXT
        );

        CREATE TABLE IF NOT EXISTS item_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            room_id INTEGER NOT NULL,
            item_id INTEGER,
            action TEXT NOT NULL,
            old_json TEXT,
            new_json TEXT,
            actor_nick TEXT,
            actor_ip TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(room_id) REFERENCES rooms(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS bans (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            scope TEXT NOT NULL, -- 'global' or 'room'
            room_id INTEGER,
            ip TEXT NOT NULL,
            reason TEXT,
            created_at TEXT NOT NULL,
            created_by_admin_id INTEGER,
            created_by_nick TEXT,
            created_by_ip TEXT,
            UNIQUE(scope, room_id, ip)
        );

        CREATE TABLE IF NOT EXISTS join_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            room_id INTEGER NOT NULL,
            requester_uid TEXT NOT NULL,
            requester_nick TEXT,
            requester_ip TEXT,
            created_at TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending', -- pending/approved/rejected
            decided_by_uid TEXT,
            decided_by_nick TEXT,
            decided_by_ip TEXT,
            decided_at TEXT,
            used_at TEXT,
            FOREIGN KEY(room_id) REFERENCES rooms(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_items_room_active ON items(room_id, is_deleted);
        CREATE INDEX IF NOT EXISTS idx_spins_room_created ON spins(room_id, created_at);
        CREATE INDEX IF NOT EXISTS idx_item_logs_room_created ON item_logs(room_id, created_at);
        CREATE INDEX IF NOT EXISTS idx_join_requests_room_status ON join_requests(room_id, status);
        CREATE INDEX IF NOT EXISTS idx_bans_ip_scope ON bans(ip, scope);
        """
    )

    # Migrations for older DBs
    _ensure_column(conn, "rooms", "review_enabled", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(conn, "items", "color", "TEXT")
    _ensure_column(conn, "items", "is_deleted", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(conn, "items", "deleted_at", "TEXT")
    _ensure_column(conn, "site_settings", "footer_text", "TEXT")
    _ensure_column(conn, "site_settings", "join_path", "TEXT")
    _ensure_column(conn, "site_settings", "join_param", "TEXT")

    # Insert default settings row
    cur.execute(
        "INSERT OR IGNORE INTO site_settings (id, site_title, site_subtitle, icon_url, icon_path, footer_text, join_path, join_param) VALUES (1, ?, ?, NULL, NULL, ?, ?, ?)",
        (
            DEFAULT_SITE_TITLE,
            DEFAULT_SITE_SUBTITLE,
            DEFAULT_FOOTER_TEXT,
            DEFAULT_JOIN_PATH,
            DEFAULT_JOIN_PARAM,
        ),
    )
    # Backfill defaults if NULL
    cur.execute(
        "UPDATE site_settings SET footer_text = COALESCE(footer_text, ?) WHERE id = 1",
        (DEFAULT_FOOTER_TEXT,),
    )
    cur.execute(
        "UPDATE site_settings SET join_path = COALESCE(join_path, ?) WHERE id = 1",
        (DEFAULT_JOIN_PATH,),
    )
    cur.execute(
        "UPDATE site_settings SET join_param = COALESCE(join_param, ?) WHERE id = 1",
        (DEFAULT_JOIN_PARAM,),
    )

    # Backfill deterministic colors for legacy items
    try:
        rows = conn.execute(
            "SELECT id FROM items WHERE color IS NULL OR TRIM(color) = ''"
        ).fetchall()
        for r in rows:
            conn.execute(
                "UPDATE items SET color = ? WHERE id = ?",
                (deterministic_pastel_color(str(r["id"])), int(r["id"])),
            )
    except Exception:
        # Do not block startup
        pass

    conn.commit()
    conn.close()


def admin_exists() -> bool:
    db = get_db()
    row = db.execute("SELECT COUNT(1) AS c FROM admin_users").fetchone()
    return bool(row and row["c"] > 0)


def is_admin_logged_in() -> bool:
    return session.get("admin_id") is not None


def ensure_user_session() -> None:
    if "uid" not in session:
        session["uid"] = uuid.uuid4().hex
    if "nick" not in session:
        session["nick"] = f"游客{session['uid'][-4:]}"


def get_client_ip() -> str:
    xff = request.headers.get("X-Forwarded-For", "")
    if xff:
        return xff.split(",")[0].strip()
    return request.remote_addr or "unknown"


def mask_ip(ip: str) -> str:
    if not ip:
        return ""
    if "." in ip:
        parts = ip.split(".")
        if len(parts) == 4:
            return f"{parts[0]}.{parts[1]}.*.*"
    if ":" in ip:
        parts = ip.split(":")
        if len(parts) >= 2:
            return f"{parts[0]}:{parts[1]}:*:*:*:*:*:*"
    return ip[:4] + "..."


def allowed_image_filename(filename: str) -> bool:
    if not filename or "." not in filename:
        return False
    ext = filename.rsplit(".", 1)[1].lower()
    return ext in ALLOWED_IMAGE_EXTS


def save_upload(file_storage, subdir: str) -> str:
    if not file_storage or not file_storage.filename:
        raise ValueError("no file")
    if not allowed_image_filename(file_storage.filename):
        raise ValueError("unsupported file type")

    safe = secure_filename(file_storage.filename)
    name = f"{uuid.uuid4().hex}_{safe}"
    rel_path = os.path.join(subdir, name)
    abs_path = os.path.join(app.config["UPLOAD_FOLDER"], rel_path)
    os.makedirs(os.path.dirname(abs_path), exist_ok=True)
    file_storage.save(abs_path)
    return rel_path.replace("\\", "/")


def get_site_settings() -> dict:
    db = get_db()
    row = db.execute("SELECT * FROM site_settings WHERE id = 1").fetchone()
    if not row:
        return {
            "site_title": DEFAULT_SITE_TITLE,
            "site_subtitle": DEFAULT_SITE_SUBTITLE,
            "footer_text": DEFAULT_FOOTER_TEXT,
            "join_path": DEFAULT_JOIN_PATH,
            "join_param": DEFAULT_JOIN_PARAM,
            "icon_url": None,
            "icon_path": None,
        }
    d = dict(row)
    d.setdefault("footer_text", DEFAULT_FOOTER_TEXT)
    d.setdefault("join_path", DEFAULT_JOIN_PATH)
    d.setdefault("join_param", DEFAULT_JOIN_PARAM)
    return d


def normalize_join_path(path: str | None) -> str:
    p = (path or "").strip() or DEFAULT_JOIN_PATH
    if not p.startswith("/"):
        p = "/" + p
    # avoid trailing spaces; allow trailing slash
    if " " in p:
        p = p.replace(" ", "")
    if p == "/":
        return DEFAULT_JOIN_PATH
    return p


def normalize_join_param(param: str | None) -> str:
    q = (param or "").strip() or DEFAULT_JOIN_PARAM
    # allow letters/numbers/underscore only
    if not re.match(r"^[A-Za-z0-9_]{1,32}$", q):
        return DEFAULT_JOIN_PARAM
    return q


def build_share_url(room_key: str) -> str:
    settings = get_site_settings()
    join_path = normalize_join_path(settings.get("join_path"))
    join_param = normalize_join_param(settings.get("join_param"))
    scheme = request.headers.get("X-Forwarded-Proto") or request.scheme
    domain = request.host
    return f"{scheme}://{domain}{join_path}?{join_param}={room_key}"


def get_room_by_key(room_key: str, conn: sqlite3.Connection | None = None):
    c = conn or get_db()
    return c.execute("SELECT * FROM rooms WHERE room_key = ?", (room_key,)).fetchone()


def ensure_room_state(room_id: int, conn: sqlite3.Connection | None = None):
    c = conn or get_db()
    c.execute("INSERT OR IGNORE INTO room_state (room_id, is_spinning) VALUES (?, 0)", (room_id,))
    if conn is None:
        c.commit()
    return c.execute("SELECT * FROM room_state WHERE room_id = ?", (room_id,)).fetchone()


def serialize_item(row: sqlite3.Row, admin_view: bool) -> dict:
    ip = row["created_by_ip"] or ""
    return {
        "id": row["id"],
        "text": row["text"],
        "image_url": row["image_url"],
        "weight": float(row["weight"] or 0),
        "color": row["color"],
        "created_by_nick": row["created_by_nick"] or "",
        "created_by_ip": ip if admin_view else mask_ip(ip),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def serialize_spin(row: sqlite3.Row, admin_view: bool) -> dict:
    ip = row["created_by_ip"] or ""
    return {
        "id": row["id"],
        "item_id": row["item_id"],
        "item_text_snapshot": row["item_text_snapshot"],
        "item_image_snapshot": row["item_image_snapshot"],
        "created_by_nick": row["created_by_nick"] or "",
        "created_by_ip": ip if admin_view else mask_ip(ip),
        "created_at": row["created_at"],
    }


def serialize_state(row: sqlite3.Row, admin_view: bool) -> dict:
    ip = row["spinning_by_ip"] or ""
    return {
        "is_spinning": bool(row["is_spinning"]),
        "spinning_by_nick": row["spinning_by_nick"] or "",
        "spinning_by_ip": ip if admin_view else mask_ip(ip),
        "spinning_started_at": row["spinning_started_at"],
        "last_spin_id": row["last_spin_id"],
    }


def serialize_join_request(row: sqlite3.Row, admin_view: bool) -> dict:
    ip = row["requester_ip"] or ""
    decided_ip = row["decided_by_ip"] or ""
    return {
        "id": row["id"],
        "requester_uid": row["requester_uid"] if admin_view else None,
        "requester_nick": row["requester_nick"] or "",
        "requester_ip": ip if admin_view else mask_ip(ip),
        "requester_ip_masked": mask_ip(ip),
        "requester_ip_full": ip,
        "created_at": row["created_at"],
        "status": row["status"],
        "decided_by_uid": row["decided_by_uid"] if admin_view else None,
        "decided_by_nick": row["decided_by_nick"] or "",
        "decided_by_ip": decided_ip if admin_view else mask_ip(decided_ip),
        "decided_at": row["decided_at"],
        "used_at": row["used_at"],
    }


def json_error(message: str, status: int = 400):
    return jsonify({"ok": False, "error": message}), status


# Real-time (SSE) event subscribers per room
SUBSCRIBERS: dict[int, list[queue.Queue]] = defaultdict(list)
SUB_LOCK = threading.Lock()

# Join-request SSE subscribers per request
JOIN_SUBSCRIBERS: dict[int, list[queue.Queue]] = defaultdict(list)
JOIN_SUB_LOCK = threading.Lock()

# Online presence (in-memory)
ONLINE: dict[int, dict[str, dict]] = defaultdict(dict)
ONLINE_LOCK = threading.Lock()


def broadcast(room_id: int, event: str, payload: dict) -> None:
    with SUB_LOCK:
        targets = list(SUBSCRIBERS.get(room_id, []))
    for q in targets:
        try:
            q.put_nowait((event, payload))
        except Exception:
            pass


def broadcast_join(request_id: int, event: str, payload: dict) -> None:
    with JOIN_SUB_LOCK:
        targets = list(JOIN_SUBSCRIBERS.get(request_id, []))
    for q in targets:
        try:
            q.put_nowait((event, payload))
        except Exception:
            pass


def touch_online(room_id: int, *, nick: str | None = None) -> None:
    uid = session.get("uid")
    if not uid:
        return

    now = time.time()
    ip = get_client_ip()
    nick_final = (nick or session.get("nick") or "").strip()[:30]

    changed = False
    with ONLINE_LOCK:
        room_map = ONLINE[room_id]
        old = room_map.get(uid)
        if not old:
            changed = True
        else:
            if old.get("nick") != nick_final:
                changed = True
            if old.get("ip") != ip:
                changed = True

        room_map[uid] = {
            "uid": uid,
            "nick": nick_final,
            "ip": ip,
            "masked_ip": mask_ip(ip),
            "is_admin": bool(is_admin_logged_in()),
            "last_seen": now,
        }

        # prune
        dead = [
            k
            for k, v in room_map.items()
            if now - float(v.get("last_seen", 0)) > ONLINE_TTL_SECONDS
        ]
        if dead:
            changed = True
            for k in dead:
                room_map.pop(k, None)

    if changed:
        broadcast(room_id, "online_changed", {"ts": utc_now_iso()})


def drop_online(room_id: int, uid: str) -> None:
    with ONLINE_LOCK:
        room_map = ONLINE.get(room_id, {})
        if uid in room_map:
            room_map.pop(uid, None)
    broadcast(room_id, "online_changed", {"ts": utc_now_iso()})


def get_online_list(room_id: int, admin_view: bool) -> list[dict]:
    now = time.time()
    result: list[dict] = []
    with ONLINE_LOCK:
        room_map = ONLINE.get(room_id, {})
        # prune lazily
        dead = [
            k
            for k, v in room_map.items()
            if now - float(v.get("last_seen", 0)) > ONLINE_TTL_SECONDS
        ]
        for k in dead:
            room_map.pop(k, None)
        for v in room_map.values():
            result.append(
                {
                    "uid": v.get("uid"),
                    "nick": v.get("nick"),
                    "ip": v.get("ip") if admin_view else v.get("masked_ip"),
                    "masked_ip": v.get("masked_ip"),
                    "is_admin": bool(v.get("is_admin")),
                    "last_seen": v.get("last_seen"),
                }
            )
    result.sort(key=lambda x: float(x.get("last_seen") or 0), reverse=True)
    return result


def _is_ip_banned(ip: str, *, room_id: int | None) -> tuple[bool, str | None]:
    if is_admin_logged_in():
        return False, None

    db = get_db()
    row = db.execute(
        "SELECT id, scope, room_id FROM bans WHERE scope = 'global' AND ip = ? LIMIT 1",
        (ip,),
    ).fetchone()
    if row:
        return True, "global"

    if room_id is not None:
        row = db.execute(
            "SELECT id, scope, room_id FROM bans WHERE scope = 'room' AND room_id = ? AND ip = ? LIMIT 1",
            (room_id, ip),
        ).fetchone()
        if row:
            return True, "room"

    return False, None


def _require_room_access(room_row: sqlite3.Row) -> tuple[bool, str | None]:
    """Return (ok, error_message). Admin always ok."""
    if is_admin_logged_in():
        return True, None

    room_id = int(room_row["id"])
    ip = get_client_ip()

    banned, scope = _is_ip_banned(ip, room_id=room_id)
    if banned:
        return False, f"你已被{'全局' if scope == 'global' else '房间'}封禁"

    # sqlite3.Row does not implement .get(); use mapping access.
    if int(room_row["review_enabled"] or 0) == 1:
        uid = session.get("uid")
        if not uid:
            return False, "未初始化会话"
        # If review is enabled *after* someone has already entered, they should not be kicked out.
        # Allow users who are currently online in this room.
        now_ts = time.time()
        with ONLINE_LOCK:
            v = (ONLINE.get(room_id) or {}).get(uid)
            if v and now_ts - float(v.get('last_seen') or 0) <= ONLINE_TTL_SECONDS:
                return True, None

        # Otherwise, only allow users who have an approved+used join request
        db = get_db()
        ok = db.execute(
            """
            SELECT 1 FROM join_requests
            WHERE room_id = ? AND requester_uid = ? AND requester_ip = ?
              AND status = 'approved' AND used_at IS NOT NULL
            ORDER BY id DESC LIMIT 1
            """,
            (room_id, uid, ip),
        ).fetchone()
        if not ok:
            return False, "房间已开启审查，请先申请加入"

    return True, None


def _item_snapshot(row: sqlite3.Row) -> dict:
    return {
        "id": int(row["id"]),
        "room_id": int(row["room_id"]),
        "text": row["text"],
        "image_url": row["image_url"],
        "weight": float(row["weight"] or 0),
        "color": row["color"],
        "is_deleted": int(row["is_deleted"] or 0),
        "deleted_at": row["deleted_at"],
        "created_by_nick": row["created_by_nick"],
        "created_by_ip": row["created_by_ip"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def log_item_action(room_id: int, item_id: int | None, action: str, old: dict | None, new: dict | None) -> None:
    db = get_db()
    actor_nick = (session.get("nick") or "").strip()[:30]
    actor_ip = get_client_ip()
    db.execute(
        "INSERT INTO item_logs (room_id, item_id, action, old_json, new_json, actor_nick, actor_ip, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            room_id,
            item_id,
            action,
            json.dumps(old, ensure_ascii=False) if old is not None else None,
            json.dumps(new, ensure_ascii=False) if new is not None else None,
            actor_nick,
            actor_ip,
            utc_now_iso(),
        ),
    )


def _ensure_not_spinning(room_id: int) -> tuple[bool, str | None]:
    db = get_db()
    state = ensure_room_state(room_id)
    if state["is_spinning"]:
        return False, "转盘正在转动，暂时禁止修改"
    return True, None


def _get_or_create_join_request(room_id: int, room_key: str) -> sqlite3.Row:
    """Create (or reuse) a pending join request for the current session."""
    db = get_db()
    uid = session.get("uid")
    ip = get_client_ip()
    nick = (session.get("nick") or "").strip()[:30]

    # Reuse existing pending request for same uid+ip
    existing = db.execute(
        """
        SELECT * FROM join_requests
        WHERE room_id = ? AND requester_uid = ? AND requester_ip = ? AND status = 'pending'
        ORDER BY id DESC LIMIT 1
        """,
        (room_id, uid, ip),
    ).fetchone()
    if existing:
        return existing

    now = utc_now_iso()
    cur = db.execute(
        "INSERT INTO join_requests (room_id, requester_uid, requester_nick, requester_ip, created_at, status) VALUES (?, ?, ?, ?, ?, 'pending')",
        (room_id, uid, nick, ip, now),
    )
    db.commit()
    rid = cur.lastrowid
    row = db.execute("SELECT * FROM join_requests WHERE id = ?", (rid,)).fetchone()

    # Notify room users
    broadcast(
        room_id,
        "join_request",
        {
            "id": rid,
            "room_key": room_key,
            "requester_nick": nick,
            "requester_ip_masked": mask_ip(ip),
            "requester_ip_full": ip,
            "created_at": now,
        },
    )
    return row


def _consume_approved_request(room_id: int) -> bool:
    """If current session has an approved unused join request, mark it used and return True."""
    if is_admin_logged_in():
        return True

    uid = session.get("uid")
    ip = get_client_ip()
    if not uid:
        return False

    db = get_db()
    row = db.execute(
        """
        SELECT * FROM join_requests
        WHERE room_id = ? AND requester_uid = ? AND requester_ip = ?
          AND status = 'approved' AND used_at IS NULL
        ORDER BY id DESC LIMIT 1
        """,
        (room_id, uid, ip),
    ).fetchone()
    if not row:
        return False

    now = utc_now_iso()
    db.execute("UPDATE join_requests SET used_at = ? WHERE id = ?", (now, int(row["id"])))
    db.commit()
    return True


@app.before_request
def setup_guard_and_session():
    ensure_user_session()

    # If admin not created yet, only allow /setup and static/uploads
    if request.endpoint in {
        "static",
        "uploaded_file",
        "setup",
    }:
        return

    if not admin_exists():
        return redirect(url_for("setup"))


@app.context_processor
def inject_globals():
    try:
        settings = get_site_settings()
    except Exception:
        settings = {
            "site_title": DEFAULT_SITE_TITLE,
            "site_subtitle": DEFAULT_SITE_SUBTITLE,
            "footer_text": DEFAULT_FOOTER_TEXT,
            "join_path": DEFAULT_JOIN_PATH,
            "join_param": DEFAULT_JOIN_PARAM,
            "icon_url": None,
            "icon_path": None,
        }
    return {
        "settings": settings,
        "is_admin": is_admin_logged_in(),
    }


@app.route("/uploads/<path:filename>")
def uploaded_file(filename: str):
    return send_from_directory(app.config["UPLOAD_FOLDER"], filename)


@app.route("/")
def index():
    return redirect(url_for("rooms"))


@app.route("/setup", methods=["GET", "POST"])
def setup():
    if admin_exists():
        return redirect(url_for("admin_login"))

    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = (request.form.get("password") or "").strip()

        if len(username) < 3:
            flash("用户名至少 3 个字符")
            return redirect(url_for("setup"))
        if len(password) < 6:
            flash("密码至少 6 个字符")
            return redirect(url_for("setup"))

        db = get_db()
        try:
            db.execute(
                "INSERT INTO admin_users (username, password_hash, created_at) VALUES (?, ?, ?)",
                (username, generate_password_hash(password), utc_now_iso()),
            )
            db.commit()
        except sqlite3.IntegrityError:
            flash("用户名已存在")
            return redirect(url_for("setup"))

        flash("管理员账号创建成功，请登录")
        return redirect(url_for("admin_login"))

    return render_template("setup.html", page_title="初始化管理员")


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if not admin_exists():
        return redirect(url_for("setup"))

    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = (request.form.get("password") or "").strip()

        db = get_db()
        row = db.execute("SELECT * FROM admin_users WHERE username = ?", (username,)).fetchone()
        if not row or not check_password_hash(row["password_hash"], password):
            flash("用户名或密码错误")
            return redirect(url_for("admin_login"))

        session["admin_id"] = row["id"]
        session["admin_username"] = row["username"]
        flash("已登录")
        return redirect(url_for("admin_dashboard"))

    return render_template("admin_login.html", page_title="管理员登录")


@app.route("/admin/logout")
def admin_logout():
    session.pop("admin_id", None)
    session.pop("admin_username", None)
    flash("已退出")
    return redirect(url_for("admin_login"))


@app.route("/admin", methods=["GET", "POST"])
def admin_dashboard():
    if not is_admin_logged_in():
        return redirect(url_for("admin_login"))

    db = get_db()

    if request.method == "POST":
        room_name = (request.form.get("room_name") or "").strip() or "未命名房间"
        alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
        for _ in range(30):
            room_key = "".join(random.choice(alphabet) for _ in range(8))
            exists = db.execute("SELECT 1 FROM rooms WHERE room_key = ?", (room_key,)).fetchone()
            if not exists:
                break
        else:
            flash("生成房间代码失败，请重试")
            return redirect(url_for("admin_dashboard"))

        db.execute(
            "INSERT INTO rooms (room_key, room_name, review_enabled, created_at) VALUES (?, ?, 0, ?)",
            (room_key, room_name[:40], utc_now_iso()),
        )
        room_id = db.execute("SELECT id FROM rooms WHERE room_key = ?", (room_key,)).fetchone()["id"]
        db.execute("INSERT OR IGNORE INTO room_state (room_id, is_spinning) VALUES (?, 0)", (room_id,))
        db.commit()

        share_url = build_share_url(room_key)
        flash(f"房间已创建：{share_url}")
        return redirect(url_for("admin_dashboard"))

    rooms = db.execute("SELECT * FROM rooms ORDER BY id DESC").fetchall()
    room_list = []
    for r in rooms:
        room_list.append(
            {
                "room_key": r["room_key"],
                "room_name": r["room_name"],
                "created_at": r["created_at"],
                "review_enabled": bool(r["review_enabled"] or 0),
                "share_url": build_share_url(r["room_key"]),
            }
        )

    return render_template("admin_dashboard.html", rooms=room_list, page_title="管理员面板")


@app.route("/admin/rooms/<room_key>")
def admin_room_manage(room_key: str):
    if not is_admin_logged_in():
        return redirect(url_for("admin_login"))

    db = get_db()
    room = get_room_by_key(room_key)
    if not room:
        flash("房间不存在")
        return redirect(url_for("admin_dashboard"))

    room_id = int(room["id"])
    share_url = build_share_url(room_key)

    # Online list uses in-memory state
    online = get_online_list(room_id, admin_view=True)

    # Bans
    room_bans = db.execute(
        "SELECT * FROM bans WHERE scope='room' AND room_id=? ORDER BY id DESC",
        (room_id,),
    ).fetchall()
    global_bans = db.execute(
        "SELECT * FROM bans WHERE scope='global' ORDER BY id DESC"
    ).fetchall()

    # Item logs
    logs = db.execute(
        "SELECT * FROM item_logs WHERE room_id=? ORDER BY id DESC LIMIT ?",
        (room_id, MAX_ITEM_LOGS),
    ).fetchall()

    return render_template(
        "admin_room.html",
        room=dict(room),
        share_url=share_url,
        online=online,
        room_bans=[dict(r) for r in room_bans],
        global_bans=[dict(r) for r in global_bans],
        logs=[dict(r) for r in logs],
        page_title=f"房间管理：{room['room_name']}",
    )


@app.route("/admin/rooms/<room_key>/delete", methods=["POST"])
def admin_room_delete(room_key: str):
    if not is_admin_logged_in():
        return redirect(url_for("admin_login"))

    db = get_db()
    room = get_room_by_key(room_key)
    if not room:
        flash("房间不存在")
        return redirect(url_for("admin_dashboard"))

    room_id = int(room["id"])
    ok, msg = _ensure_not_spinning(room_id)
    if not ok:
        flash(msg)
        return redirect(url_for("admin_room_manage", room_key=room_key))

    db.execute("DELETE FROM rooms WHERE id = ?", (room_id,))
    db.commit()
    flash(f"已删除房间：{room_key}")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/rooms/<room_key>/review", methods=["POST"])
def admin_room_toggle_review(room_key: str):
    if not is_admin_logged_in():
        return redirect(url_for("admin_login"))

    db = get_db()
    room = get_room_by_key(room_key)
    if not room:
        flash("房间不存在")
        return redirect(url_for("admin_dashboard"))

    room_id = int(room["id"])
    enabled = int(room["review_enabled"] or 0)
    new_val = 0 if enabled else 1
    db.execute("UPDATE rooms SET review_enabled=? WHERE id=?", (new_val, room_id))
    db.commit()

    broadcast(room_id, "review_changed", {"review_enabled": bool(new_val), "ts": utc_now_iso()})

    flash("已{}审查模式".format("开启" if new_val else "关闭"))
    return redirect(url_for("admin_room_manage", room_key=room_key))


@app.route("/admin/rooms/<room_key>/ban", methods=["POST"])
def admin_room_add_ban(room_key: str):
    if not is_admin_logged_in():
        return redirect(url_for("admin_login"))

    db = get_db()
    room = get_room_by_key(room_key)
    if not room:
        flash("房间不存在")
        return redirect(url_for("admin_dashboard"))

    room_id = int(room["id"])
    scope = (request.form.get("scope") or "room").strip()
    ip = (request.form.get("ip") or "").strip()
    reason = (request.form.get("reason") or "").strip()[:120] or None

    if not ip:
        flash("IP 不能为空")
        return redirect(url_for("admin_room_manage", room_key=room_key))
    if scope not in {"room", "global"}:
        scope = "room"

    db.execute(
        "INSERT OR IGNORE INTO bans (scope, room_id, ip, reason, created_at, created_by_admin_id, created_by_nick, created_by_ip) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            scope,
            room_id if scope == "room" else None,
            ip,
            reason,
            utc_now_iso(),
            session.get("admin_id"),
            session.get("admin_username"),
            get_client_ip(),
        ),
    )
    db.commit()

    # kick anyone matching this IP in this room
    with ONLINE_LOCK:
        room_map = ONLINE.get(room_id, {})
        to_kick = [uid for uid, v in room_map.items() if v.get("ip") == ip]
    for uid in to_kick:
        broadcast(room_id, "kicked", {"target_uid": uid, "reason": "你已被封禁"})
        drop_online(room_id, uid)

    broadcast(room_id, "bans_changed", {"ts": utc_now_iso()})

    flash("已添加封禁")
    return redirect(url_for("admin_room_manage", room_key=room_key))


@app.route("/admin/rooms/<room_key>/unban/<int:ban_id>", methods=["POST"])
def admin_room_unban(room_key: str, ban_id: int):
    if not is_admin_logged_in():
        return redirect(url_for("admin_login"))

    db = get_db()
    db.execute("DELETE FROM bans WHERE id = ?", (ban_id,))
    db.commit()

    room = get_room_by_key(room_key)
    if room:
        broadcast(int(room["id"]), "bans_changed", {"ts": utc_now_iso()})

    flash("已解除封禁")
    return redirect(url_for("admin_room_manage", room_key=room_key))


@app.route("/admin/rooms/<room_key>/kick", methods=["POST"])
def admin_room_kick(room_key: str):
    if not is_admin_logged_in():
        return redirect(url_for("admin_login"))

    db = get_db()
    room = get_room_by_key(room_key)
    if not room:
        flash("房间不存在")
        return redirect(url_for("admin_dashboard"))

    room_id = int(room["id"])
    target_uid = (request.form.get("uid") or "").strip()
    action = (request.form.get("action") or "kick").strip()

    # find target info from ONLINE
    with ONLINE_LOCK:
        info = ONLINE.get(room_id, {}).get(target_uid)

    if not info:
        flash("用户不在线或已离开")
        return redirect(url_for("admin_room_manage", room_key=room_key))

    target_ip = info.get("ip")
    if target_uid == session.get("uid"):
        flash("不能对自己执行此操作")
        return redirect(url_for("admin_room_manage", room_key=room_key))

    if action == "kick":
        broadcast(room_id, "kicked", {"target_uid": target_uid, "reason": "你已被管理员踢出"})
        drop_online(room_id, target_uid)
        flash("已踢出")
        return redirect(url_for("admin_room_manage", room_key=room_key))

    # ban_room / ban_global
    scope = "room" if action == "ban_room" else "global"
    db.execute(
        "INSERT OR IGNORE INTO bans (scope, room_id, ip, reason, created_at, created_by_admin_id, created_by_nick, created_by_ip) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            scope,
            room_id if scope == "room" else None,
            target_ip,
            "kicked+ban",
            utc_now_iso(),
            session.get("admin_id"),
            session.get("admin_username"),
            get_client_ip(),
        ),
    )
    db.commit()

    broadcast(room_id, "kicked", {"target_uid": target_uid, "reason": "你已被封禁"})
    drop_online(room_id, target_uid)
    broadcast(room_id, "bans_changed", {"ts": utc_now_iso()})

    flash("已封禁并踢出")
    return redirect(url_for("admin_room_manage", room_key=room_key))


@app.route("/admin/rooms/<room_key>/logs/revert/<int:log_id>", methods=["POST"])
def admin_room_revert_log(room_key: str, log_id: int):
    if not is_admin_logged_in():
        return redirect(url_for("admin_login"))

    db = get_db()
    room = get_room_by_key(room_key)
    if not room:
        flash("房间不存在")
        return redirect(url_for("admin_dashboard"))

    room_id = int(room["id"])
    ok, msg = _ensure_not_spinning(room_id)
    if not ok:
        flash(msg)
        return redirect(url_for("admin_room_manage", room_key=room_key))

    log = db.execute(
        "SELECT * FROM item_logs WHERE id=? AND room_id=?",
        (log_id, room_id),
    ).fetchone()
    if not log:
        flash("日志不存在")
        return redirect(url_for("admin_room_manage", room_key=room_key))

    old_json = log["old_json"]
    action = log["action"]
    item_id = log["item_id"]
    if not item_id:
        flash("该日志无法回滚")
        return redirect(url_for("admin_room_manage", room_key=room_key))

    if not old_json and action != "create":
        flash("该日志没有可用的旧数据")
        return redirect(url_for("admin_room_manage", room_key=room_key))

    now = utc_now_iso()

    if action == "create":
        # Undo create: soft delete
        existing = db.execute(
            "SELECT * FROM items WHERE id=? AND room_id=?",
            (item_id, room_id),
        ).fetchone()
        if not existing:
            flash("项目不存在")
            return redirect(url_for("admin_room_manage", room_key=room_key))
        old_snap = _item_snapshot(existing)
        db.execute(
            "UPDATE items SET is_deleted=1, deleted_at=?, updated_at=? WHERE id=? AND room_id=?",
            (now, now, item_id, room_id),
        )
        db.commit()
        log_item_action(room_id, int(item_id), "revert_create", old_snap, None)
        db.commit()
        broadcast(room_id, "items_changed", {"ts": now})
        flash("已撤销创建（软删除）")
        return redirect(url_for("admin_room_manage", room_key=room_key))

    # Parse old state and apply
    try:
        old_state = json.loads(old_json)
    except Exception:
        flash("旧数据解析失败")
        return redirect(url_for("admin_room_manage", room_key=room_key))

    existing = db.execute(
        "SELECT * FROM items WHERE id=? AND room_id=?",
        (item_id, room_id),
    ).fetchone()
    if not existing:
        flash("项目不存在")
        return redirect(url_for("admin_room_manage", room_key=room_key))

    before = _item_snapshot(existing)

    db.execute(
        """
        UPDATE items
        SET text=?, image_url=?, weight=?, color=?, is_deleted=?, deleted_at=?, updated_at=?
        WHERE id=? AND room_id=?
        """,
        (
            (old_state.get("text") or existing["text"])[:200],
            old_state.get("image_url"),
            float(old_state.get("weight") or 0),
            normalize_hex_color(old_state.get("color")) or existing["color"],
            int(old_state.get("is_deleted") or 0),
            old_state.get("deleted_at"),
            now,
            item_id,
            room_id,
        ),
    )
    db.commit()

    after_row = db.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
    after = _item_snapshot(after_row) if after_row else None
    log_item_action(room_id, int(item_id), "revert", before, after)
    db.commit()

    broadcast(room_id, "items_changed", {"ts": now})
    flash("已回滚项目")
    return redirect(url_for("admin_room_manage", room_key=room_key))


@app.route("/admin/settings", methods=["GET", "POST"])
def admin_settings():
    if not is_admin_logged_in():
        return redirect(url_for("admin_login"))

    db = get_db()
    settings = get_site_settings()

    if request.method == "POST":
        site_title = (request.form.get("site_title") or "").strip()[:60] or DEFAULT_SITE_TITLE
        site_subtitle = (request.form.get("site_subtitle") or "").strip()[:120]
        footer_text = (request.form.get("footer_text") or "").strip()[:200] or DEFAULT_FOOTER_TEXT

        join_path = normalize_join_path(request.form.get("join_path") or DEFAULT_JOIN_PATH)
        join_param = normalize_join_param(request.form.get("join_param") or DEFAULT_JOIN_PARAM)

        # Safety: avoid conflicting with built-in routes.
        forbidden_prefixes = ("/admin", "/api", "/static", "/uploads", "/setup")
        if any(join_path.startswith(p) for p in forbidden_prefixes):
            flash("加入路径不能以 /admin /api /static /uploads /setup 开头（会与系统路由冲突）")
            return redirect(url_for("admin_settings"))

        icon_url = (request.form.get("icon_url") or "").strip()[:500] or None

        icon_path = settings.get("icon_path")
        icon_file = request.files.get("icon_file")
        if icon_file and icon_file.filename:
            try:
                icon_path = save_upload(icon_file, "icons")
                if not icon_url:
                    icon_url = None
            except Exception as e:
                flash(f"上传 icon 失败：{e}")
                return redirect(url_for("admin_settings"))

        db.execute(
            """
            UPDATE site_settings
            SET site_title=?, site_subtitle=?, icon_url=?, icon_path=?, footer_text=?, join_path=?, join_param=?
            WHERE id=1
            """,
            (site_title, site_subtitle, icon_url, icon_path, footer_text, join_path, join_param),
        )
        db.commit()
        flash("已保存站点设置")
        return redirect(url_for("admin_settings"))

    return render_template("admin_settings.html", settings=settings, page_title="站点设置")


@app.route("/kicked")
def kicked_page():
    room_key = (request.args.get("key") or "").strip()
    share_url = None
    if room_key:
        share_url = build_share_url(room_key)
    return render_template(
        "kicked.html",
        room_key=room_key,
        share_url=share_url,
        page_title="已被踢出",
    )


@app.route("/rooms")
def rooms():
    settings = get_site_settings()
    join_param = normalize_join_param(settings.get("join_param"))

    room_key = (request.args.get("key") or request.args.get(join_param) or "").strip()
    if not room_key:
        return render_template(
            "room_missing.html",
            message="请输入房间代码进入房间，或联系管理员获取分享链接。",
            page_title="进入房间",
        )

    room = get_room_by_key(room_key)
    if not room:
        return render_template(
            "room_missing.html",
            message=f"找不到房间：{room_key}",
            page_title="房间不存在",
        )

    room_id = int(room["id"])

    # Ban check
    ip = get_client_ip()
    banned, scope = _is_ip_banned(ip, room_id=room_id)
    if banned:
        return (
            render_template(
                "banned.html",
                message=f"你已被{'全局' if scope == 'global' else '房间'}封禁。",
                page_title="已封禁",
            ),
            403,
        )

    # Review check (admin bypass)
    if int(room["review_enabled"] or 0) == 1 and not is_admin_logged_in():
        # If approved request exists, consume it and allow entry
        if not _consume_approved_request(room_id):
            req = _get_or_create_join_request(room_id, room_key)
            share_url = build_share_url(room_key)
            return render_template(
                "join_wait.html",
                room=dict(room),
                share_url=share_url,
                request_id=int(req["id"]),
                page_title="等待审核",
            )

    state = ensure_room_state(room_id)

    touch_online(room_id)  # mark online

    share_url = build_share_url(room_key)

    admin_view = bool(is_admin_logged_in())
    me = {
        "uid": session.get("uid"),
        "nick": session.get("nick"),
        "ip": get_client_ip() if admin_view else mask_ip(get_client_ip()),
        "masked_ip": mask_ip(get_client_ip()),
        "is_admin": admin_view,
    }

    return render_template(
        "room.html",
        room=dict(room),
        share_url=share_url,
        me_json=json.dumps(me, ensure_ascii=False),
        page_title=f"房间：{room['room_name']}",
    )


@app.route("/rooms/<room_key>")
def room_short(room_key: str):
    return redirect(url_for("rooms", key=room_key))


@app.route("/api/rooms/<room_key>/snapshot")
def api_room_snapshot(room_key: str):
    room = get_room_by_key(room_key)
    if not room:
        return json_error("room not found", 404)

    ok, msg = _require_room_access(room)
    if not ok:
        return json_error(msg or "forbidden", 403)

    touch_online(int(room["id"]))

    db = get_db()
    admin_view = bool(is_admin_logged_in())

    state = ensure_room_state(int(room["id"]))
    items = db.execute(
        "SELECT * FROM items WHERE room_id=? AND is_deleted=0 ORDER BY id ASC",
        (int(room["id"]),),
    ).fetchall()
    spins = db.execute(
        "SELECT * FROM spins WHERE room_id=? ORDER BY id DESC LIMIT ?",
        (int(room["id"]), MAX_SPIN_RECORDS),
    ).fetchall()

    pending_requests: list[dict] = []
    if int(room["review_enabled"] or 0) == 1:
        reqs = db.execute(
            "SELECT * FROM join_requests WHERE room_id=? AND status='pending' ORDER BY id DESC LIMIT 50",
            (int(room["id"]),),
        ).fetchall()
        pending_requests = [serialize_join_request(r, admin_view) for r in reqs]

    payload = {
        "ok": True,
        "room": {
            "room_key": room["room_key"],
            "room_name": room["room_name"],
            "review_enabled": bool(room["review_enabled"] or 0),
        },
        "me": {
            "uid": session.get("uid"),
            "nick": session.get("nick"),
            "ip": get_client_ip() if admin_view else mask_ip(get_client_ip()),
            "masked_ip": mask_ip(get_client_ip()),
            "is_admin": admin_view,
        },
        "state": serialize_state(state, admin_view),
        "items": [serialize_item(r, admin_view) for r in items],
        "spins": [serialize_spin(r, admin_view) for r in spins],
        "online": get_online_list(int(room["id"]), admin_view),
        "join_requests": pending_requests,
    }
    return jsonify(payload)


@app.route("/api/rooms/<room_key>/items", methods=["GET", "POST"])
def api_items(room_key: str):
    room = get_room_by_key(room_key)
    if not room:
        return json_error("room not found", 404)

    ok, msg = _require_room_access(room)
    if not ok:
        return json_error(msg or "forbidden", 403)

    touch_online(int(room["id"]))

    db = get_db()
    admin_view = bool(is_admin_logged_in())

    if request.method == "GET":
        items = db.execute(
            "SELECT * FROM items WHERE room_id=? AND is_deleted=0 ORDER BY id ASC",
            (int(room["id"]),),
        ).fetchall()
        return jsonify({"ok": True, "items": [serialize_item(r, admin_view) for r in items]})

    room_id = int(room["id"])

    # POST add item
    ok2, msg2 = _ensure_not_spinning(room_id)
    if not ok2:
        return json_error(msg2 or "room is spinning", 409)

    text = (request.form.get("text") or "").strip()
    if not text:
        return json_error("text is required")

    try:
        weight = float(request.form.get("weight") or 1)
    except ValueError:
        return json_error("invalid weight")
    if weight < 0:
        return json_error("weight must be >= 0")

    color = normalize_hex_color(request.form.get("color"))
    if not color:
        color = random_pastel_color()

    image_url = (request.form.get("image_url") or "").strip()[:500] or None

    image_file = request.files.get("image_file")
    if image_file and image_file.filename:
        try:
            rel_path = save_upload(image_file, "items")
            image_url = url_for("uploaded_file", filename=rel_path)
        except Exception as e:
            return json_error(f"upload failed: {e}")

    now = utc_now_iso()
    ip = get_client_ip()
    nick = (session.get("nick") or "").strip()[:30]

    cur = db.execute(
        """
        INSERT INTO items (room_id, text, image_url, weight, color, is_deleted, deleted_at, created_by_nick, created_by_ip, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, 0, NULL, ?, ?, ?, ?)
        """,
        (room_id, text[:200], image_url, weight, color, nick, ip, now, now),
    )
    db.commit()

    item_id = int(cur.lastrowid)
    item_row = db.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()

    try:
        log_item_action(room_id, item_id, "create", None, _item_snapshot(item_row))
        db.commit()
    except Exception:
        pass

    broadcast(room_id, "items_changed", {"ts": now})
    return jsonify({"ok": True, "item": serialize_item(item_row, admin_view)})


@app.route("/api/rooms/<room_key>/items/<int:item_id>", methods=["PATCH", "DELETE", "POST"])
def api_item_update_delete(room_key: str, item_id: int):
    room = get_room_by_key(room_key)
    if not room:
        return json_error("room not found", 404)

    ok, msg = _require_room_access(room)
    if not ok:
        return json_error(msg or "forbidden", 403)

    touch_online(int(room["id"]))

    db = get_db()
    admin_view = bool(is_admin_logged_in())

    method = request.method
    if method == "POST":
        override = (request.args.get("_method") or "").upper().strip()
        if override in {"PATCH", "DELETE"}:
            method = override

    room_id = int(room["id"])
    ok2, msg2 = _ensure_not_spinning(room_id)
    if not ok2:
        return json_error(msg2 or "room is spinning", 409)

    existing = db.execute(
        "SELECT * FROM items WHERE id=? AND room_id=?",
        (item_id, room_id),
    ).fetchone()
    if not existing:
        return json_error("item not found", 404)

    if method == "DELETE":
        now = utc_now_iso()
        old_snap = _item_snapshot(existing)
        db.execute(
            "UPDATE items SET is_deleted=1, deleted_at=?, updated_at=? WHERE id=? AND room_id=?",
            (now, now, item_id, room_id),
        )
        db.commit()
        try:
            log_item_action(room_id, item_id, "delete", old_snap, None)
            db.commit()
        except Exception:
            pass
        broadcast(room_id, "items_changed", {"ts": now})
        return jsonify({"ok": True})

    # PATCH update
    now = utc_now_iso()

    text = None
    weight = None
    image_url = None
    color = None
    clear_image = False

    if request.is_json:
        data = request.get_json(silent=True) or {}
        if "text" in data:
            text = (data.get("text") or "").strip()
        if "weight" in data:
            try:
                weight = float(data.get("weight"))
            except Exception:
                return json_error("invalid weight")
        if "image_url" in data:
            image_url = (data.get("image_url") or "").strip()[:500] or None
        if "color" in data:
            color = normalize_hex_color(data.get("color"))
            if color is None and (data.get("color") or "").strip():
                return json_error("invalid color; use #RRGGBB")
        if data.get("clear_image"):
            clear_image = True
    else:
        if "text" in request.form:
            text = (request.form.get("text") or "").strip()
        if "weight" in request.form:
            try:
                weight = float(request.form.get("weight") or 0)
            except Exception:
                return json_error("invalid weight")
        if "image_url" in request.form:
            image_url = (request.form.get("image_url") or "").strip()[:500] or None
        if "color" in request.form:
            raw = request.form.get("color")
            if (raw or "").strip():
                color = normalize_hex_color(raw)
                if not color:
                    return json_error("invalid color; use #RRGGBB")
        if request.form.get("clear_image") == "1":
            clear_image = True

        image_file = request.files.get("image_file")
        if image_file and image_file.filename:
            try:
                rel_path = save_upload(image_file, "items")
                image_url = url_for("uploaded_file", filename=rel_path)
            except Exception as e:
                return json_error(f"upload failed: {e}")

    if text is not None and not text:
        return json_error("text cannot be empty")
    if weight is not None and weight < 0:
        return json_error("weight must be >= 0")

    new_text = text[:200] if text is not None else existing["text"]
    new_weight = float(weight) if weight is not None else float(existing["weight"] or 0)

    if clear_image:
        new_image = None
    elif image_url is not None:
        new_image = image_url
    else:
        new_image = existing["image_url"]

    new_color = color if color is not None else existing["color"]

    old_snap = _item_snapshot(existing)

    db.execute(
        "UPDATE items SET text=?, image_url=?, weight=?, color=?, updated_at=? WHERE id=? AND room_id=?",
        (new_text, new_image, new_weight, new_color, now, item_id, room_id),
    )
    db.commit()

    item_row = db.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()

    try:
        log_item_action(room_id, item_id, "update", old_snap, _item_snapshot(item_row))
        db.commit()
    except Exception:
        pass

    broadcast(room_id, "items_changed", {"ts": now})
    return jsonify({"ok": True, "item": serialize_item(item_row, admin_view)})


@app.route("/api/rooms/<room_key>/spins")
def api_spins(room_key: str):
    room = get_room_by_key(room_key)
    if not room:
        return json_error("room not found", 404)

    ok, msg = _require_room_access(room)
    if not ok:
        return json_error(msg or "forbidden", 403)

    touch_online(int(room["id"]))

    db = get_db()
    admin_view = bool(is_admin_logged_in())
    spins = db.execute(
        "SELECT * FROM spins WHERE room_id=? ORDER BY id DESC LIMIT ?",
        (int(room["id"]), MAX_SPIN_RECORDS),
    ).fetchall()
    return jsonify({"ok": True, "spins": [serialize_spin(r, admin_view) for r in spins]})


def _finalize_spin(room_id: int, spin_id: int) -> None:
    time.sleep(SPIN_DURATION_SECONDS)

    conn = connect_db()
    try:
        conn.execute(
            "UPDATE room_state SET is_spinning=0, spinning_by_nick=NULL, spinning_by_ip=NULL, spinning_started_at=NULL, last_spin_id=? WHERE room_id=?",
            (spin_id, room_id),
        )
        conn.commit()
    finally:
        conn.close()

    broadcast(room_id, "state_changed", {"ts": utc_now_iso()})
    broadcast(room_id, "spin_end", {"spin_id": spin_id, "ts": utc_now_iso()})


@app.route("/api/rooms/<room_key>/spin", methods=["POST"])
def api_spin(room_key: str):
    room = get_room_by_key(room_key)
    if not room:
        return json_error("room not found", 404)

    ok, msg = _require_room_access(room)
    if not ok:
        return json_error(msg or "forbidden", 403)

    touch_online(int(room["id"]))

    admin_view = bool(is_admin_logged_in())

    db = get_db()
    try:
        db.execute("BEGIN IMMEDIATE")
    except sqlite3.OperationalError:
        return json_error("database busy, please retry", 503)

    state = ensure_room_state(int(room["id"]))
    if state["is_spinning"]:
        db.execute("ROLLBACK")
        return json_error("room is already spinning", 409)

    items = db.execute(
        "SELECT * FROM items WHERE room_id=? AND is_deleted=0 AND weight > 0 ORDER BY id ASC",
        (int(room["id"]),),
    ).fetchall()

    if not items:
        db.execute("ROLLBACK")
        return json_error("no items with weight > 0", 400)

    weights = [float(r["weight"] or 0) for r in items]
    total = sum(weights)
    if total <= 0:
        db.execute("ROLLBACK")
        return json_error("total weight must be > 0", 400)

    pick = random.random() * total
    acc = 0.0
    chosen = items[-1]
    for r in items:
        acc += float(r["weight"] or 0)
        if pick <= acc:
            chosen = r
            break

    now = utc_now_iso()
    ip = get_client_ip()
    nick = (session.get("nick") or "").strip()[:30]

    db.execute(
        "UPDATE room_state SET is_spinning=1, spinning_by_nick=?, spinning_by_ip=?, spinning_started_at=? WHERE room_id=?",
        (nick, ip, now, int(room["id"])),
    )

    cur = db.execute(
        "INSERT INTO spins (room_id, item_id, item_text_snapshot, item_image_snapshot, created_by_nick, created_by_ip, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            int(room["id"]),
            chosen["id"],
            chosen["text"],
            chosen["image_url"],
            nick,
            ip,
            now,
        ),
    )
    spin_id = int(cur.lastrowid)
    db.commit()

    broadcast(int(room["id"]), "state_changed", {"ts": now})
    broadcast(
        int(room["id"]),
        "spin_start",
        {
            "spin_id": spin_id,
            "selected_item_id": chosen["id"],
            "started_at": now,
            "duration": SPIN_DURATION_SECONDS,
            "by_nick": nick,
            "by_ip": ip if admin_view else mask_ip(ip),
        },
    )
    broadcast(int(room["id"]), "spins_changed", {"ts": now})

    t = threading.Thread(target=_finalize_spin, args=(int(room["id"]), spin_id), daemon=True)
    t.start()

    return jsonify(
        {
            "ok": True,
            "spin_id": spin_id,
            "selected_item_id": chosen["id"],
            "started_at": now,
            "duration": SPIN_DURATION_SECONDS,
        }
    )


@app.route("/api/rooms/<room_key>/heartbeat", methods=["POST"])
def api_heartbeat(room_key: str):
    room = get_room_by_key(room_key)
    if not room:
        return json_error("room not found", 404)

    ok, msg = _require_room_access(room)
    if not ok:
        return json_error(msg or "forbidden", 403)

    data = request.get_json(silent=True) or {}
    nick = (data.get("nick") or "").strip()[:30]
    if nick:
        session["nick"] = nick

    touch_online(int(room["id"]), nick=nick or None)

    admin_view = bool(is_admin_logged_in())
    return jsonify({"ok": True, "online": get_online_list(int(room["id"]), admin_view)})


@app.route("/api/rooms/<room_key>/join_requests")
def api_join_requests(room_key: str):
    room = get_room_by_key(room_key)
    if not room:
        return json_error("room not found", 404)

    ok, msg = _require_room_access(room)
    if not ok:
        return json_error(msg or "forbidden", 403)

    db = get_db()
    admin_view = bool(is_admin_logged_in())
    reqs = db.execute(
        "SELECT * FROM join_requests WHERE room_id=? AND status='pending' ORDER BY id DESC LIMIT 50",
        (int(room["id"]),),
    ).fetchall()
    return jsonify({"ok": True, "join_requests": [serialize_join_request(r, admin_view) for r in reqs]})


@app.route("/api/rooms/<room_key>/join_requests/<int:req_id>/decide", methods=["POST"])
def api_join_request_decide(room_key: str, req_id: int):
    room = get_room_by_key(room_key)
    if not room:
        return json_error("room not found", 404)

    ok, msg = _require_room_access(room)
    if not ok:
        return json_error(msg or "forbidden", 403)

    db = get_db()
    room_id = int(room["id"])

    data = request.get_json(silent=True) or {}
    decision = (data.get("decision") or "").strip().lower()
    if decision not in {"approve", "reject"}:
        return json_error("invalid decision")

    uid = session.get("uid")
    ip = get_client_ip()
    nick = (session.get("nick") or "").strip()[:30]

    # must be "in room" (online) to decide
    with ONLINE_LOCK:
        is_in_room = uid in ONLINE.get(room_id, {})
    if not is_in_room:
        return json_error("only online room members can decide", 403)

    req = db.execute(
        "SELECT * FROM join_requests WHERE id=? AND room_id=?",
        (req_id, room_id),
    ).fetchone()
    if not req:
        return json_error("request not found", 404)

    if req["requester_uid"] == uid:
        return json_error("requester cannot decide", 403)

    if req["status"] != "pending":
        # already decided
        return jsonify({"ok": True, "status": req["status"], "already_decided": True})

    status = "approved" if decision == "approve" else "rejected"
    now = utc_now_iso()

    # First decision wins
    db.execute(
        """
        UPDATE join_requests
        SET status=?, decided_by_uid=?, decided_by_nick=?, decided_by_ip=?, decided_at=?
        WHERE id=? AND room_id=? AND status='pending'
        """,
        (status, uid, nick, ip, now, req_id, room_id),
    )
    db.commit()

    decided = db.execute("SELECT * FROM join_requests WHERE id=?", (req_id,)).fetchone()

    # Notify room users
    broadcast(
        room_id,
        "join_request_decided",
        {
            "id": req_id,
            "status": status,
            "decided_by_nick": nick,
            "decided_at": now,
        },
    )
    # Notify requester waiting page
    broadcast_join(
        req_id,
        "decided",
        {
            "id": req_id,
            "status": status,
            "decided_by_nick": nick,
            "decided_at": now,
            "room_key": room_key,
        },
    )

    return jsonify({"ok": True, "status": status})


@app.route("/api/join_requests/<int:req_id>/status")
def api_join_request_status(req_id: int):
    db = get_db()
    req = db.execute("SELECT * FROM join_requests WHERE id=?", (req_id,)).fetchone()
    if not req:
        return json_error("request not found", 404)

    uid = session.get("uid")
    if not is_admin_logged_in() and uid != req["requester_uid"]:
        return json_error("forbidden", 403)

    return jsonify({"ok": True, "status": req["status"], "room_id": req["room_id"], "room_key": None})


@app.route("/api/join_requests/<int:req_id>/events")
def api_join_request_events(req_id: int):
    db = get_db()
    req = db.execute("SELECT * FROM join_requests WHERE id=?", (req_id,)).fetchone()
    if not req:
        return json_error("request not found", 404)

    uid = session.get("uid")
    if not is_admin_logged_in() and uid != req["requester_uid"]:
        return json_error("forbidden", 403)

    q: queue.Queue = queue.Queue(maxsize=100)
    with JOIN_SUB_LOCK:
        JOIN_SUBSCRIBERS[req_id].append(q)

    def gen():
        try:
            yield "event: hello\ndata: {}\n\n"
            # if already decided, push immediately
            if req["status"] != "pending":
                payload = json.dumps({"id": req_id, "status": req["status"]}, ensure_ascii=False)
                yield f"event: decided\ndata: {payload}\n\n"
            while True:
                try:
                    event, payload = q.get(timeout=15)
                    data = json.dumps(payload, ensure_ascii=False)
                    yield f"event: {event}\ndata: {data}\n\n"
                except queue.Empty:
                    yield ": keepalive\n\n"
        finally:
            with JOIN_SUB_LOCK:
                lst = JOIN_SUBSCRIBERS.get(req_id, [])
                if q in lst:
                    lst.remove(q)

    resp = Response(stream_with_context(gen()), mimetype="text/event-stream")
    resp.headers["Cache-Control"] = "no-cache"
    resp.headers["X-Accel-Buffering"] = "no"
    return resp


@app.route("/api/rooms/<room_key>/admin/kick", methods=["POST"])
def api_admin_kick(room_key: str):
    if not is_admin_logged_in():
        return json_error("admin required", 403)

    room = get_room_by_key(room_key)
    if not room:
        return json_error("room not found", 404)

    room_id = int(room["id"])

    data = request.get_json(silent=True) or {}
    target_uid = (data.get("target_uid") or "").strip()
    ban_scope = (data.get("ban_scope") or "").strip()  # '', 'room', 'global'
    reason = (data.get("reason") or "").strip()[:120] or None

    if not target_uid:
        return json_error("target_uid required")

    if target_uid == session.get("uid"):
        return json_error("cannot kick yourself")

    with ONLINE_LOCK:
        info = ONLINE.get(room_id, {}).get(target_uid)
    if not info:
        return json_error("user not online", 404)

    target_ip = info.get("ip")

    # optional ban
    if ban_scope in {"room", "global"}:
        db = get_db()
        db.execute(
            "INSERT OR IGNORE INTO bans (scope, room_id, ip, reason, created_at, created_by_admin_id, created_by_nick, created_by_ip) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                ban_scope,
                room_id if ban_scope == "room" else None,
                target_ip,
                reason,
                utc_now_iso(),
                session.get("admin_id"),
                session.get("admin_username"),
                get_client_ip(),
            ),
        )
        db.commit()

    broadcast(room_id, "kicked", {"target_uid": target_uid, "reason": reason or "你已被管理员踢出"})
    drop_online(room_id, target_uid)
    if ban_scope in {"room", "global"}:
        broadcast(room_id, "bans_changed", {"ts": utc_now_iso()})

    return jsonify({"ok": True})


@app.route("/api/rooms/<room_key>/events")
def api_events(room_key: str):
    conn = connect_db()
    try:
        room = get_room_by_key(room_key, conn=conn)
    finally:
        conn.close()

    if not room:
        return json_error("room not found", 404)

    ok, msg = _require_room_access(room)
    if not ok:
        return json_error(msg or "forbidden", 403)

    touch_online(int(room["id"]))

    q: queue.Queue = queue.Queue(maxsize=200)
    room_id = int(room["id"])

    with SUB_LOCK:
        SUBSCRIBERS[room_id].append(q)

    def gen():
        try:
            yield "event: hello\ndata: {}\n\n"
            while True:
                try:
                    event, payload = q.get(timeout=15)
                    data = json.dumps(payload, ensure_ascii=False)
                    yield f"event: {event}\ndata: {data}\n\n"
                except queue.Empty:
                    yield ": keepalive\n\n"
        finally:
            with SUB_LOCK:
                lst = SUBSCRIBERS.get(room_id, [])
                if q in lst:
                    lst.remove(q)

    resp = Response(stream_with_context(gen()), mimetype="text/event-stream")
    resp.headers["Cache-Control"] = "no-cache"
    resp.headers["X-Accel-Buffering"] = "no"
    return resp


@app.route("/<path:catchall>")
def custom_join_path(catchall: str):
    # This enables the admin-configured custom join path (e.g. /wdf/wdf?rmn=XXXX).
    # We only treat it as a room entry path when it matches the current join_path.
    settings = get_site_settings()
    join_path = normalize_join_path(settings.get("join_path"))
    # Flask provides catchall without leading '/'
    current_path = "/" + catchall

    if current_path == join_path:
        join_param = normalize_join_param(settings.get("join_param"))
        room_key = (request.args.get(join_param) or request.args.get("key") or "").strip()
        if room_key:
            return redirect(url_for("rooms", key=room_key))
        return render_template(
            "room_missing.html",
            message="缺少房间代码参数。",
            page_title="进入房间",
        )

    abort(404)


if __name__ == "__main__":
    init_db()
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=True, threaded=True)
else:
    init_db()
