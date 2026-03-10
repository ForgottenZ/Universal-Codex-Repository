#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, time
from pathlib import Path
from typing import Any

import requests
import yaml
from flask import Flask, redirect, render_template, request, url_for, flash
from flask_sqlalchemy import SQLAlchemy
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
import msal

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.yaml"

DEFAULT_CONFIG = {
    "app": {"secret_key": "replace-this-secret"},
    "user": {"username": "hax", "nickname": "Hax"},
    "teaching_calendar": {
        "term_name": "2025-2026学年(春)",
        "start_date": "2025-03-03",
        "end_date": "2025-05-11",
    },
    "database": {
        "mysql": {
            "enabled": False,
            "host": "127.0.0.1",
            "port": 3306,
            "user": "root",
            "password": "",
            "database": "teaching_calendar",
        }
    },
    "notify": {
        "pushdeer": {"enabled": False, "pushkey": ""},
        "microsoft_graph": {
            "enabled": False,
            "tenant_id": "common",
            "client_id": "",
            "client_secret": "",
            "sender_email": "",
            "scopes": ["https://graph.microsoft.com/.default"],
        },
    },
    "weekly_report": {
        "enabled": True,
        "future_weeks": 3,
        "schedules": [
            {"weekday": "mon", "time": "12:00"}
        ],
        "template": "Hi, {UserNickname}!\\n现在是第{NowTeachWeek}教学周！{ProgressBar} {NowTeachWeek}/{MaxTeachWeek}\\n您本周的事件有：\\n{CurrentWeekEvents}\\n您未来{FutureWeeks}周的事件有：\\n{FutureEvents}\\n详细说明：\\n{Notes}",
        "email_to": "",
    },
}


def ensure_config():
    if not CONFIG_PATH.exists():
        CONFIG_PATH.write_text(yaml.safe_dump(DEFAULT_CONFIG, allow_unicode=True, sort_keys=False), encoding="utf-8")


def load_config() -> dict:
    ensure_config()
    return yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))


CONFIG = load_config()

app = Flask(__name__)
app.config["SECRET_KEY"] = CONFIG["app"]["secret_key"]


mysql = CONFIG["database"]["mysql"]
if mysql.get("enabled"):
    db_uri = f"mysql+pymysql://{mysql['user']}:{mysql['password']}@{mysql['host']}:{mysql['port']}/{mysql['database']}?charset=utf8mb4"
else:
    db_uri = f"sqlite:///{BASE_DIR / 'calendar.db'}"

app.config["SQLALCHEMY_DATABASE_URI"] = db_uri
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db = SQLAlchemy(app)


class Event(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(128), nullable=False)
    event_type = db.Column(db.String(20), nullable=False, default="one_time")
    note = db.Column(db.Text, default="")

    start_week = db.Column(db.Integer, nullable=False)
    start_weekday = db.Column(db.Integer, nullable=False)  # 1-7
    start_hour = db.Column(db.Integer, nullable=False, default=0)
    start_minute = db.Column(db.Integer, nullable=False, default=0)

    end_week = db.Column(db.Integer, nullable=True)
    end_weekday = db.Column(db.Integer, nullable=True)
    end_hour = db.Column(db.Integer, nullable=True)
    end_minute = db.Column(db.Integer, nullable=True)

    repeat_weeks = db.Column(db.Integer, nullable=True)
    repeat_days = db.Column(db.Integer, nullable=True)
    repeat_hours = db.Column(db.Integer, nullable=True)
    repeat_minutes = db.Column(db.Integer, nullable=True)

    remind_push = db.Column(db.Boolean, default=False)
    remind_email = db.Column(db.Boolean, default=False)
    remind_text = db.Column(db.Text, default="")


class NotifyLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    kind = db.Column(db.String(32), nullable=False)  # event/weekly_report
    event_id = db.Column(db.Integer, nullable=True)
    unique_key = db.Column(db.String(128), unique=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


@dataclass
class TeachTime:
    week: int
    weekday: int
    hour: int
    minute: int


def term_dates():
    c = load_config()["teaching_calendar"]
    start = datetime.strptime(c["start_date"], "%Y-%m-%d").date()
    end = datetime.strptime(c["end_date"], "%Y-%m-%d").date()
    return start, end


def max_teach_week():
    s, e = term_dates()
    return ((e - s).days // 7) + 1


def to_teach_time(dt: datetime) -> TeachTime:
    s, _ = term_dates()
    d = dt.date()
    delta = (d - s).days
    return TeachTime(week=delta // 7 + 1, weekday=dt.weekday() + 1, hour=dt.hour, minute=dt.minute)


def teach_time_to_dt(tt: TeachTime) -> datetime:
    s, _ = term_dates()
    d = s + timedelta(days=(tt.week - 1) * 7 + (tt.weekday - 1))
    return datetime.combine(d, time(tt.hour, tt.minute))


def parse_hhmm(v: str):
    h, m = v.split(":")
    return int(h), int(m)


def event_matches_now(event: Event, now: datetime) -> bool:
    t = to_teach_time(now)
    if event.event_type == "one_time":
        return (t.week, t.weekday, t.hour, t.minute) == (
            event.start_week,
            event.start_weekday,
            event.start_hour,
            event.start_minute,
        )

    if event.event_type == "recurring":
        start_dt = teach_time_to_dt(TeachTime(event.start_week, event.start_weekday, event.start_hour, event.start_minute))
        if now < start_dt:
            return False
        diff = int((now - start_dt).total_seconds() // 60)
        interval = (event.repeat_weeks or 0) * 7 * 24 * 60 + (event.repeat_days or 0) * 24 * 60 + (event.repeat_hours or 0) * 60 + (event.repeat_minutes or 0)
        if interval <= 0:
            return False
        return diff % interval == 0

    return False


def event_is_happening(event: Event, now: datetime) -> bool:
    if event.event_type != "range":
        return False
    start_dt = teach_time_to_dt(TeachTime(event.start_week, event.start_weekday, event.start_hour, event.start_minute))
    end_dt = teach_time_to_dt(TeachTime(event.end_week, event.end_weekday, event.end_hour, event.end_minute))
    return start_dt <= now <= end_dt


def progress_bar(current: int, maximum: int) -> str:
    ratio = 0 if maximum <= 0 else min(max(current / maximum, 0), 1)
    full = round(ratio * 10)
    return "[" + "■" * full + "□" * (10 - full) + "]"


def send_pushdeer(text: str):
    cfg = load_config()["notify"]["pushdeer"]
    if not cfg.get("enabled"):
        return
    if not cfg.get("pushkey"):
        return
    requests.get("https://api2.pushdeer.com/message/push", params={"pushkey": cfg["pushkey"], "text": "教学周提醒", "desp": text}, timeout=10)


def graph_token(cfg: dict) -> str | None:
    app_client = msal.ConfidentialClientApplication(
        client_id=cfg["client_id"],
        client_credential=cfg["client_secret"],
        authority=f"https://login.microsoftonline.com/{cfg['tenant_id']}",
    )
    token = app_client.acquire_token_for_client(scopes=cfg["scopes"])
    return token.get("access_token")


def send_graph_email(text: str):
    cfg = load_config()["notify"]["microsoft_graph"]
    if not cfg.get("enabled"):
        return
    token = graph_token(cfg)
    if not token:
        return
    payload = {
        "message": {
            "subject": "教学周提醒",
            "body": {"contentType": "Text", "content": text},
            "toRecipients": [{"emailAddress": {"address": cfg["sender_email"]}}],
        }
    }
    requests.post(
        f"https://graph.microsoft.com/v1.0/users/{cfg['sender_email']}/sendMail",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        data=json.dumps(payload),
        timeout=15,
    )


def send_notification(content: str, use_push: bool, use_email: bool):
    if use_push:
        send_pushdeer(content)
    if use_email:
        send_graph_email(content)


def notify_due_events():
    now = datetime.now().replace(second=0, microsecond=0)
    for event in Event.query.all():
        matched = event_matches_now(event, now)
        if matched:
            key = f"event-{event.id}-{now.strftime('%Y%m%d%H%M')}"
            if NotifyLog.query.filter_by(unique_key=key).first():
                continue
            text = event.remind_text or f"事件提醒：{event.name}"
            send_notification(text, event.remind_push, event.remind_email)
            db.session.add(NotifyLog(kind="event", event_id=event.id, unique_key=key))
            db.session.commit()


def format_event_brief(event: Event, now_week: int) -> str:
    remind = []
    if event.remind_push:
        remind.append("Pushdeer")
    if event.remind_email:
        remind.append("Email")
    remind_text = "启用提醒（" + "，".join(remind) + "）" if remind else "未启用提醒"
    if event.event_type == "one_time":
        return f"{event.name}：第{event.start_week}教学周，周{event.start_weekday}{event.start_hour:02d}:{event.start_minute:02d}，{remind_text}，单次。"
    if event.event_type == "recurring":
        return f"{event.name}：周{event.start_weekday}{event.start_hour:02d}:{event.start_minute:02d}起，{remind_text}，循环。"
    return f"{event.name}：第{event.start_week}教学周-第{event.end_week}教学周{progress_bar(now_week - event.start_week + 1, event.end_week - event.start_week + 1)} {max(now_week - event.start_week + 1, 0)}/{event.end_week - event.start_week + 1}，覆盖。"


def render_weekly_report() -> str:
    cfg = load_config()
    user = cfg["user"]
    report_cfg = cfg["weekly_report"]
    now = datetime.now()
    now_tt = to_teach_time(now)
    current_week = now_tt.week
    future_weeks = int(report_cfg.get("future_weeks", 3))
    events = Event.query.all()
    this_week = []
    future = []
    notes = []
    for e in events:
        if e.note:
            notes.append(f"{e.name}：{e.note}")
        if e.event_type == "range":
            if e.start_week <= current_week <= (e.end_week or e.start_week):
                this_week.append(format_event_brief(e, current_week))
            elif current_week < e.start_week <= current_week + future_weeks:
                future.append(format_event_brief(e, current_week))
            continue
        if e.start_week == current_week:
            this_week.append(format_event_brief(e, current_week))
        elif current_week < e.start_week <= current_week + future_weeks:
            future.append(format_event_brief(e, current_week))
        elif e.event_type == "recurring":
            this_week.append(format_event_brief(e, current_week))
            future.append(format_event_brief(e, current_week))

    vars_map = {
        "UserNickname": user.get("nickname", "User"),
        "NowTeachWeek": current_week,
        "MaxTeachWeek": max_teach_week(),
        "ProgressBar": progress_bar(current_week, max_teach_week()),
        "FutureWeeks": future_weeks,
        "CurrentWeekEvents": "\\n".join(f"{i+1}.{x}" for i, x in enumerate(this_week)) or "无",
        "FutureEvents": "\\n".join(f"{i+1}.{x}" for i, x in enumerate(future)) or "无",
        "Notes": "\\n".join(notes) or "（如果没有备注将不会在此处列出）",
    }
    tpl = report_cfg.get("template", "")
    return tpl.format_map(vars_map)


def weekly_report_job(schedule_key: str):
    now = datetime.now()
    key = f"weekly-{schedule_key}-{now.strftime('%Y%W')}"
    if NotifyLog.query.filter_by(unique_key=key).first():
        return
    report = render_weekly_report()
    send_notification(report, True, True)
    db.session.add(NotifyLog(kind="weekly_report", unique_key=key))
    db.session.commit()


def setup_scheduler():
    scheduler = BackgroundScheduler()
    scheduler.add_job(notify_due_events, "interval", minutes=1, id="event_scanner")
    report_cfg = load_config()["weekly_report"]
    if report_cfg.get("enabled"):
        for i, rule in enumerate(report_cfg.get("schedules", [])):
            weekday = str(rule.get("weekday", "mon")).lower()[:3]
            hh, mm = parse_hhmm(rule.get("time", "12:00"))
            trigger = CronTrigger(day_of_week=weekday, hour=hh, minute=mm)
            scheduler.add_job(lambda k=f"{i}-{weekday}-{hh:02d}{mm:02d}": weekly_report_job(k), trigger, id=f"weekly_{i}")
    scheduler.start()


@app.route("/")
def index():
    cfg = load_config()
    events = Event.query.order_by(Event.start_week, Event.start_weekday, Event.start_hour).all()
    s, _ = term_dates()
    cal = []
    for w in range(1, max_teach_week() + 1):
        row = []
        for d in range(1, 8):
            dt = s + timedelta(days=(w - 1) * 7 + d - 1)
            row.append({"date": dt.day, "month": dt.month})
        cal.append({"week": w, "days": row})
    return render_template("index.html", cfg=cfg, events=events, cal=cal)


@app.post("/config")
def save_config():
    cfg = load_config()
    cfg["user"]["username"] = request.form["username"]
    cfg["user"]["nickname"] = request.form["nickname"]
    cfg["teaching_calendar"]["term_name"] = request.form["term_name"]
    cfg["teaching_calendar"]["start_date"] = request.form["start_date"]
    cfg["teaching_calendar"]["end_date"] = request.form["end_date"]
    cfg["notify"]["pushdeer"]["enabled"] = request.form.get("push_enabled") == "on"
    cfg["notify"]["pushdeer"]["pushkey"] = request.form.get("pushkey", "")
    cfg["weekly_report"]["template"] = request.form.get("weekly_template", cfg["weekly_report"]["template"])
    CONFIG_PATH.write_text(yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False), encoding="utf-8")
    flash("配置已保存（重启程序后新的周报计划生效）")
    return redirect(url_for("index"))


@app.post("/events")
def create_event():
    e = Event(
        name=request.form["name"],
        event_type=request.form["event_type"],
        start_week=int(request.form["start_week"]),
        start_weekday=int(request.form["start_weekday"]),
        start_hour=int(request.form["start_hour"]),
        start_minute=int(request.form["start_minute"]),
        end_week=int(request.form.get("end_week") or 0) or None,
        end_weekday=int(request.form.get("end_weekday") or 0) or None,
        end_hour=int(request.form.get("end_hour") or 0) if request.form.get("end_hour") else None,
        end_minute=int(request.form.get("end_minute") or 0) if request.form.get("end_minute") else None,
        repeat_weeks=int(request.form.get("repeat_weeks") or 0) or None,
        repeat_days=int(request.form.get("repeat_days") or 0) or None,
        repeat_hours=int(request.form.get("repeat_hours") or 0) or None,
        repeat_minutes=int(request.form.get("repeat_minutes") or 0) or None,
        remind_push=request.form.get("remind_push") == "on",
        remind_email=request.form.get("remind_email") == "on",
        remind_text=request.form.get("remind_text", ""),
        note=request.form.get("note", ""),
    )
    db.session.add(e)
    db.session.commit()
    flash("事件已创建")
    return redirect(url_for("index"))


@app.post("/events/<int:event_id>/delete")
def delete_event(event_id):
    e = Event.query.get_or_404(event_id)
    db.session.delete(e)
    db.session.commit()
    flash("事件已删除")
    return redirect(url_for("index"))


with app.app_context():
    db.create_all()

setup_scheduler()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
