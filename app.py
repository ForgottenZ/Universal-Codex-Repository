import os
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import requests
import yaml
from flask import Flask, jsonify, redirect, render_template_string, request, url_for
from flask_sqlalchemy import SQLAlchemy
from apscheduler.schedulers.background import BackgroundScheduler
from msal import PublicClientApplication, SerializableTokenCache

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.yaml"
DEFAULT_SQLITE_PATH = BASE_DIR / "teaching_calendar.db"
PUSHDEER_API = "https://api2.pushdeer.com/message/push"


def default_config() -> dict:
    return {
        "app": {"host": "0.0.0.0", "port": 8000, "secret_key": "change-me"},
        "database": {
            "mysql": {
                "enabled": False,
                "host": "127.0.0.1",
                "port": 3306,
                "user": "root",
                "password": "",
                "database": "teaching_calendar",
            },
            "sqlite": {"path": str(DEFAULT_SQLITE_PATH)},
        },
        "term": {
            "name": "2025-2026学年(春)",
            "start": "2025-03-03 00:00",
            "end": "2025-07-20 23:59",
        },
        "report": {
            "enabled": True,
            "times": ["Mon 12:00"],
            "lookahead_weeks": 3,
            "template": (
                "现在是第{{current_week}}教学周。\n"
                "本周事件：\n{{this_week_events}}\n\n"
                "未来{{lookahead_weeks}}周事件：\n{{upcoming_events}}"
            ),
        },
        "notify": {
            "pushdeer": {"enabled": False, "pushkey": ""},
            "graph": {
                "enabled": False,
                "client_id": "",
                "tenant": "consumers",
                "from_addr": "",
                "to_addrs": [],
                "scopes": ["Mail.Send", "User.Read", "offline_access"],
                "cache_file": str(BASE_DIR / "msal_cache.bin"),
            },
        },
    }


def ensure_config() -> dict:
    if not CONFIG_PATH.exists():
        cfg = default_config()
        CONFIG_PATH.write_text(yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False), encoding="utf-8")
        return cfg
    return yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))


CFG = ensure_config()
app = Flask(__name__)
app.secret_key = CFG["app"].get("secret_key", "change-me")


def db_uri_from_config(cfg: dict) -> str:
    mysql = cfg.get("database", {}).get("mysql", {})
    if mysql.get("enabled"):
        return (
            f"mysql+pymysql://{mysql.get('user')}:{mysql.get('password')}@"
            f"{mysql.get('host')}:{mysql.get('port')}/{mysql.get('database')}?charset=utf8mb4"
        )
    sqlite_path = cfg.get("database", {}).get("sqlite", {}).get("path", str(DEFAULT_SQLITE_PATH))
    return f"sqlite:///{sqlite_path}"


app.config["SQLALCHEMY_DATABASE_URI"] = db_uri_from_config(CFG)
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db = SQLAlchemy(app)


class Event(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text, default="")
    notify_on_trigger = db.Column(db.Boolean, default=True)
    notify_text = db.Column(db.Text, default="")
    start_at = db.Column(db.DateTime, nullable=False)
    end_at = db.Column(db.DateTime, nullable=True)
    repeat_weeks = db.Column(db.Integer, default=0)
    repeat_days = db.Column(db.Integer, default=0)
    repeat_hours = db.Column(db.Integer, default=0)
    repeat_minutes = db.Column(db.Integer, default=0)
    is_overlay = db.Column(db.Boolean, default=False)
    last_triggered_at = db.Column(db.DateTime, nullable=True)


class ReportLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    sent_at = db.Column(db.DateTime, nullable=False)
    schedule_key = db.Column(db.String(50), nullable=False)


@dataclass
class TermInfo:
    name: str
    start: datetime
    end: datetime


def parse_dt(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%d %H:%M")


def get_term() -> TermInfo:
    t = CFG["term"]
    return TermInfo(name=t["name"], start=parse_dt(t["start"]), end=parse_dt(t["end"]))


def to_teaching_pos(dt: datetime) -> tuple[int, int, int, int]:
    term = get_term()
    diff = dt - term.start
    if diff.total_seconds() < 0:
        return 0, 0, dt.hour, dt.minute
    total_days = diff.days
    week = total_days // 7 + 1
    weekday = total_days % 7 + 1
    return week, weekday, dt.hour, dt.minute


def from_teaching_pos(week: int, weekday: int, hour: int, minute: int) -> datetime:
    term = get_term()
    return term.start + timedelta(days=(week - 1) * 7 + (weekday - 1), hours=hour, minutes=minute)


def format_event(e: Event) -> str:
    return f"- {e.title} @ {e.start_at:%Y-%m-%d %H:%M}"


def render_report() -> str:
    now = datetime.now()
    current_week, _, _, _ = to_teaching_pos(now)
    lookahead = int(CFG["report"].get("lookahead_weeks", 3))
    start = now
    end = now + timedelta(weeks=lookahead)
    this_week_end = now + timedelta(days=(7 - now.weekday()))

    this_week_events = Event.query.filter(Event.start_at >= start, Event.start_at <= this_week_end).all()
    upcoming_events = Event.query.filter(Event.start_at > this_week_end, Event.start_at <= end).all()

    ctx = {
        "current_week": current_week,
        "lookahead_weeks": lookahead,
        "this_week_events": "\n".join(format_event(x) for x in this_week_events) or "(无)",
        "upcoming_events": "\n".join(format_event(x) for x in upcoming_events) or "(无)",
        "now": now.strftime("%Y-%m-%d %H:%M"),
    }
    text = CFG["report"].get("template", "")
    for k, v in ctx.items():
        text = text.replace("{{" + k + "}}", str(v))
    return text


def send_pushdeer(text: str):
    conf = CFG["notify"]["pushdeer"]
    if not conf.get("enabled") or not conf.get("pushkey"):
        return
    requests.post(PUSHDEER_API, data={"pushkey": conf["pushkey"], "text": "教学周提醒", "desp": text}, timeout=10)


def acquire_graph_token() -> str | None:
    g = CFG["notify"]["graph"]
    if not g.get("enabled") or not g.get("client_id"):
        return None
    cache = SerializableTokenCache()
    cache_path = Path(g.get("cache_file", BASE_DIR / "msal_cache.bin"))
    if cache_path.exists():
        cache.deserialize(cache_path.read_text(encoding="utf-8"))
    app_msal = PublicClientApplication(
        client_id=g["client_id"], authority=f"https://login.microsoftonline.com/{g.get('tenant', 'consumers')}", token_cache=cache
    )
    scopes = g.get("scopes", ["Mail.Send", "User.Read", "offline_access"])
    accounts = app_msal.get_accounts()
    result = app_msal.acquire_token_silent(scopes=scopes, account=accounts[0]) if accounts else None
    if not result:
        flow = app_msal.initiate_device_flow(scopes=scopes)
        print(flow.get("message", "请完成微软设备码登录"))
        result = app_msal.acquire_token_by_device_flow(flow)
    if cache.has_state_changed:
        cache_path.write_text(cache.serialize(), encoding="utf-8")
    return result.get("access_token") if result else None


def send_graph_mail(subject: str, body: str):
    g = CFG["notify"]["graph"]
    token = acquire_graph_token()
    if not token:
        return
    to_addrs = g.get("to_addrs") or []
    if not to_addrs:
        return
    payload = {
        "message": {
            "subject": subject,
            "body": {"contentType": "Text", "content": body},
            "toRecipients": [{"emailAddress": {"address": x}} for x in to_addrs],
            "from": {"emailAddress": {"address": g.get("from_addr", "")}},
        },
        "saveToSentItems": True,
    }
    requests.post(
        "https://graph.microsoft.com/v1.0/me/sendMail",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json=payload,
        timeout=10,
    )


def send_notify(text: str):
    send_pushdeer(text)
    send_graph_mail("教学周提醒", text)


def next_occurrence(e: Event, now: datetime) -> datetime | None:
    if e.is_overlay:
        return e.start_at if e.start_at <= now <= (e.end_at or e.start_at) else None
    if e.repeat_weeks == e.repeat_days == e.repeat_hours == e.repeat_minutes == 0:
        return e.start_at if e.start_at <= now else None
    step = timedelta(weeks=e.repeat_weeks, days=e.repeat_days, hours=e.repeat_hours, minutes=e.repeat_minutes)
    if step.total_seconds() <= 0:
        return e.start_at if e.start_at <= now else None
    occ = e.start_at
    while occ <= now:
        if abs((now - occ).total_seconds()) < 60:
            return occ
        occ = occ + step
    return None


def check_events_and_reports():
    with app.app_context():
        now = datetime.now()
        for e in Event.query.all():
            occ = next_occurrence(e, now)
            if not occ:
                continue
            if e.last_triggered_at and abs((e.last_triggered_at - occ).total_seconds()) < 60:
                continue
            if e.notify_on_trigger:
                send_notify(e.notify_text or f"事件触发: {e.title} ({occ:%Y-%m-%d %H:%M})")
            e.last_triggered_at = occ
            db.session.add(e)

        if CFG["report"].get("enabled"):
            for item in CFG["report"].get("times", ["Mon 12:00"]):
                key = item.strip()
                parts = key.split()
                if len(parts) != 2:
                    continue
                day_name, hm = parts
                hh, mm = [int(x) for x in hm.split(":")]
                day_map = {"Mon": 0, "Tue": 1, "Wed": 2, "Thu": 3, "Fri": 4, "Sat": 5, "Sun": 6}
                if now.weekday() == day_map.get(day_name) and now.hour == hh and now.minute == mm:
                    exist = ReportLog.query.filter_by(schedule_key=key).order_by(ReportLog.sent_at.desc()).first()
                    if exist and exist.sent_at.date() == now.date():
                        continue
                    send_notify(render_report())
                    db.session.add(ReportLog(sent_at=now, schedule_key=key))

        db.session.commit()


PAGE = """
<!doctype html><html><head><meta charset='utf-8'><title>教学周日历系统</title>
<style>body{font-family:Arial;padding:20px}table{border-collapse:collapse;width:100%}th,td{border:1px solid #cfd8e3;padding:8px;text-align:center}th{background:#eaf2f8}.week{font-weight:bold;background:#f7f9fb}.sun,.sat{color:#ff6a00}</style>
</head><body>
<h1>{{term.name}}</h1>
<p>起止：{{term.start}} - {{term.end}}</p>
<h2>教学周日历</h2>
<table><tr><th>教学周</th><th>周一</th><th>周二</th><th>周三</th><th>周四</th><th>周五</th><th class='sat'>周六</th><th class='sun'>周日</th></tr>
{% for row in rows %}<tr><td class='week'>第{{row.week}}教学周</td>{% for d in row.days %}<td class='{{d.cls}}'>{{d.label}}</td>{% endfor %}</tr>{% endfor %}
</table>
<h2>新增事件</h2>
<form method='post' action='/events'>
标题<input name='title' required> 说明<input name='description'>
开始(第w周/周d/h/m)<input name='week' type='number' min='1' required><input name='weekday' type='number' min='1' max='7' required><input name='hour' type='number' min='0' max='23' required><input name='minute' type='number' min='0' max='59' required>
结束(可选，覆盖事件)<input name='end_week' type='number' min='1'><input name='end_weekday' type='number' min='1' max='7'><input name='end_hour' type='number' min='0' max='23'><input name='end_minute' type='number' min='0' max='59'>
提醒<input type='checkbox' name='notify_on_trigger' checked> 提醒文本<input name='notify_text'>
循环(周/日/时/分)<input name='repeat_weeks' type='number' min='0' value='0'><input name='repeat_days' type='number' min='0' value='0'><input name='repeat_hours' type='number' min='0' value='0'><input name='repeat_minutes' type='number' min='0' value='0'>
<button type='submit'>保存事件</button></form>
<h2>事件列表</h2><ul>{% for e in events %}<li>#{{e.id}} {{e.title}} | {{e.start_at}}{% if e.end_at %} ~ {{e.end_at}}{% endif %} | 循环:{{e.repeat_weeks}}w {{e.repeat_days}}d {{e.repeat_hours}}h {{e.repeat_minutes}}m | 覆盖:{{e.is_overlay}} <a href='/events/{{e.id}}/delete'>删除</a></li>{% endfor %}</ul>
<h2>周报预览</h2><pre>{{report_preview}}</pre>
</body></html>
"""


@app.route("/")
def index():
    term = get_term()
    total_weeks = ((term.end - term.start).days // 7) + 1
    rows = []
    for w in range(1, total_weeks + 1):
        days = []
        for d in range(1, 8):
            cur = from_teaching_pos(w, d, 0, 0)
            days.append({"label": cur.day, "cls": "sat" if d == 6 else "sun" if d == 7 else ""})
        rows.append({"week": w, "days": days})
    events = Event.query.order_by(Event.start_at.asc()).all()
    return render_template_string(PAGE, term=term, rows=rows, events=events, report_preview=render_report())


@app.route("/events", methods=["POST"])
def add_event():
    f = request.form
    start = from_teaching_pos(int(f["week"]), int(f["weekday"]), int(f["hour"]), int(f["minute"]))
    end_at = None
    is_overlay = False
    if f.get("end_week") and f.get("end_weekday") and f.get("end_hour") and f.get("end_minute"):
        end_at = from_teaching_pos(int(f["end_week"]), int(f["end_weekday"]), int(f["end_hour"]), int(f["end_minute"]))
        is_overlay = True
    event = Event(
        title=f["title"],
        description=f.get("description", ""),
        notify_on_trigger=bool(f.get("notify_on_trigger")),
        notify_text=f.get("notify_text", ""),
        start_at=start,
        end_at=end_at,
        repeat_weeks=int(f.get("repeat_weeks") or 0),
        repeat_days=int(f.get("repeat_days") or 0),
        repeat_hours=int(f.get("repeat_hours") or 0),
        repeat_minutes=int(f.get("repeat_minutes") or 0),
        is_overlay=is_overlay,
    )
    db.session.add(event)
    db.session.commit()
    return redirect(url_for("index"))


@app.route("/events/<int:event_id>/delete")
def delete_event(event_id: int):
    e = Event.query.get_or_404(event_id)
    db.session.delete(e)
    db.session.commit()
    return redirect(url_for("index"))


@app.route("/api/report/preview")
def report_preview():
    return jsonify({"preview": render_report()})


@app.route("/api/config", methods=["GET", "POST"])
def config_api():
    global CFG
    if request.method == "GET":
        return jsonify(CFG)
    CFG = request.get_json(force=True)
    CONFIG_PATH.write_text(yaml.safe_dump(CFG, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return jsonify({"ok": True})


def create_app():
    with app.app_context():
        db.create_all()
    scheduler = BackgroundScheduler()
    scheduler.add_job(check_events_and_reports, "interval", minutes=1)
    scheduler.start()
    return app


if __name__ == "__main__":
    create_app().run(host=CFG["app"].get("host", "0.0.0.0"), port=int(CFG["app"].get("port", 8000)))
