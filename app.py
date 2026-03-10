#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json
import os
from datetime import datetime, date, time, timedelta
from pathlib import Path
from typing import Optional

import requests
import yaml
import msal
from flask import Flask, redirect, render_template_string, request, url_for, flash
from flask_sqlalchemy import SQLAlchemy
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.yaml"

DEFAULT_CONFIG = {
    "app": {
        "host": "0.0.0.0",
        "port": 8000,
        "secret_key": "change-me",
        "timezone": "Asia/Shanghai",
    },
    "user": {"username": "Hax", "user_nickname": "Hax"},
    "term": {
        "name": "2025-2026学年(春)",
        "start_date": "2025-02-24",
        "end_date": "2025-05-04",
        "max_teach_week": 10,
    },
    "database": {"enable_mysql": False, "mysql_url": "mysql+pymysql://user:pass@127.0.0.1:3306/teach_calendar"},
    "notify": {
        "pushdeer": {"enable": False, "pushkey": ""},
        "microsoft_graph": {
            "enable": False,
            "tenant_id": "common",
            "client_id": "",
            "sender_email": "",
            "recipient_email": "",
            "token_cache": "graph_token_cache.bin",
        },
    },
    "report": {
        "enable": True,
        "future_weeks": 3,
        "schedules": ["Mon 12:00"],
        "template": (
            "Hi, {UserNickname}!\\n"
            "现在是第{NowTeachWeek}教学周！{WeekProgressBar} {NowTeachWeek}/{MaxTeachWeek}\\n"
            "您本周的事件有：\\n{CurrentWeekEvents}\\n"
            "您未来{FutureWeeks}周的事件有：\\n{FutureEvents}\\n"
            "详细说明：\\n{EventNotes}"
        ),
    },
}


def ensure_config() -> dict:
    if not CONFIG_PATH.exists():
        CONFIG_PATH.write_text(yaml.safe_dump(DEFAULT_CONFIG, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))


def save_config(cfg: dict):
    CONFIG_PATH.write_text(yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False), encoding="utf-8")


cfg = ensure_config()
app = Flask(__name__)
app.config["SECRET_KEY"] = cfg["app"]["secret_key"]

if cfg["database"].get("enable_mysql") and cfg["database"].get("mysql_url"):
    db_uri = cfg["database"]["mysql_url"]
else:
    db_uri = f"sqlite:///{BASE_DIR / 'teach_calendar.db'}"
app.config["SQLALCHEMY_DATABASE_URI"] = db_uri
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db = SQLAlchemy(app)
scheduler = BackgroundScheduler(timezone=cfg["app"].get("timezone", "Asia/Shanghai"))


class Event(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(128), nullable=False)
    event_type = db.Column(db.String(16), nullable=False)  # single/recurring/coverage
    note = db.Column(db.Text, default="")

    # single
    teach_week = db.Column(db.Integer)
    weekday = db.Column(db.Integer)  # 1-7
    hour = db.Column(db.Integer)
    minute = db.Column(db.Integer)

    # recurring base + interval
    r_start_week = db.Column(db.Integer)
    r_start_weekday = db.Column(db.Integer)
    r_start_hour = db.Column(db.Integer)
    r_start_minute = db.Column(db.Integer)
    r_every_weeks = db.Column(db.Integer)
    r_every_days = db.Column(db.Integer)
    r_every_hours = db.Column(db.Integer)
    r_every_minutes = db.Column(db.Integer)

    # coverage
    c_start_week = db.Column(db.Integer)
    c_start_weekday = db.Column(db.Integer)
    c_start_hour = db.Column(db.Integer)
    c_start_minute = db.Column(db.Integer)
    c_end_week = db.Column(db.Integer)
    c_end_weekday = db.Column(db.Integer)
    c_end_hour = db.Column(db.Integer)
    c_end_minute = db.Column(db.Integer)

    reminder_enable = db.Column(db.Boolean, default=False)
    remind_pushdeer = db.Column(db.Boolean, default=False)
    remind_email = db.Column(db.Boolean, default=False)


class NotificationLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    event_id = db.Column(db.Integer, nullable=False)
    trigger_at = db.Column(db.DateTime, nullable=False)
    sent_at = db.Column(db.DateTime, default=datetime.now)


class ReportLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    schedule_key = db.Column(db.String(32), nullable=False)
    week_index = db.Column(db.Integer, nullable=False)
    sent_at = db.Column(db.DateTime, default=datetime.now)


def term_start() -> date:
    return datetime.strptime(cfg["term"]["start_date"], "%Y-%m-%d").date()


def term_end() -> date:
    return datetime.strptime(cfg["term"]["end_date"], "%Y-%m-%d").date()


def teach_to_datetime(week: int, weekday: int, hour: int, minute: int) -> datetime:
    return datetime.combine(term_start() + timedelta(weeks=week - 1, days=weekday - 1), time(hour=hour, minute=minute))


def current_teach_week(now: Optional[datetime] = None) -> int:
    now = now or datetime.now()
    if now.date() < term_start():
        return 0
    return ((now.date() - term_start()).days // 7) + 1


def progress_bar(curr: int, max_week: int) -> str:
    if max_week <= 0:
        return "[□□□□□□□□□□]"
    filled = min(10, max(0, int((curr / max_week) * 10)))
    return "[" + "■" * filled + "□" * (10 - filled) + "]"


def event_trigger_time(ev: Event) -> Optional[datetime]:
    if ev.event_type == "single":
        return teach_to_datetime(ev.teach_week, ev.weekday, ev.hour, ev.minute)
    return None


def in_coverage(ev: Event, now: datetime) -> bool:
    start = teach_to_datetime(ev.c_start_week, ev.c_start_weekday, ev.c_start_hour, ev.c_start_minute)
    end = teach_to_datetime(ev.c_end_week, ev.c_end_weekday, ev.c_end_hour, ev.c_end_minute)
    return start <= now <= end


def should_fire_recurring(ev: Event, now: datetime) -> bool:
    start = teach_to_datetime(ev.r_start_week, ev.r_start_weekday, ev.r_start_hour, ev.r_start_minute)
    delta = timedelta(weeks=ev.r_every_weeks or 0, days=ev.r_every_days or 0, hours=ev.r_every_hours or 0, minutes=ev.r_every_minutes or 0)
    if delta.total_seconds() <= 0 or now < start:
        return False
    diff = now - start
    return diff.total_seconds() % delta.total_seconds() < 60


def send_pushdeer(title: str, body: str):
    ncfg = cfg["notify"]["pushdeer"]
    if not ncfg.get("enable") or not ncfg.get("pushkey"):
        return
    requests.post("https://api2.pushdeer.com/message/push", json={"pushkey": ncfg["pushkey"], "text": title, "desp": body}, timeout=10)


def send_graph_email(subject: str, body: str):
    mcfg = cfg["notify"]["microsoft_graph"]
    if not mcfg.get("enable"):
        return
    cache = msal.SerializableTokenCache()
    cache_path = BASE_DIR / mcfg.get("token_cache", "graph_token_cache.bin")
    if cache_path.exists():
        cache.deserialize(cache_path.read_text(encoding="utf-8"))
    app_msal = msal.PublicClientApplication(client_id=mcfg["client_id"], authority=f"https://login.microsoftonline.com/{mcfg['tenant_id']}", token_cache=cache)
    scopes = ["Mail.Send", "offline_access", "User.Read"]
    accounts = app_msal.get_accounts()
    token = app_msal.acquire_token_silent(scopes, account=accounts[0] if accounts else None)
    if not token:
        flow = app_msal.initiate_device_flow(scopes=scopes)
        if "user_code" not in flow:
            return
        print(f"请访问 {flow['verification_uri']} 输入代码 {flow['user_code']} 完成 Graph 登录")
        token = app_msal.acquire_token_by_device_flow(flow)
    if cache.has_state_changed:
        cache_path.write_text(cache.serialize(), encoding="utf-8")
    if "access_token" not in token:
        return
    recipient = mcfg.get("recipient_email") or mcfg.get("sender_email")
    requests.post(
        "https://graph.microsoft.com/v1.0/me/sendMail",
        headers={"Authorization": f"Bearer {token['access_token']}", "Content-Type": "application/json"},
        json={
            "message": {
                "subject": subject,
                "body": {"contentType": "Text", "content": body},
                "toRecipients": [{"emailAddress": {"address": recipient}}],
            },
            "saveToSentItems": "true",
        },
        timeout=15,
    )


def format_event_line(ev: Event, now_week: int) -> str:
    if ev.event_type == "single":
        return f"{ev.name}：第{ev.teach_week}教学周，周{ev.weekday} {ev.hour:02d}:{ev.minute:02d}，{'启用提醒' if ev.reminder_enable else '未启用提醒'}，单次。"
    if ev.event_type == "recurring":
        return f"{ev.name}：每{ev.r_every_weeks or 0}教学周/{ev.r_every_days or 0}日/{ev.r_every_hours or 0}小时/{ev.r_every_minutes or 0}分钟，{'启用提醒' if ev.reminder_enable else '未启用提醒'}，循环。"
    p = min(10, max(0, int((now_week / max(ev.c_end_week, 1)) * 10)))
    bar = '[' + '■' * p + '□' * (10 - p) + ']'
    return f"{ev.name}：第{ev.c_start_week}教学周-第{ev.c_end_week}教学周{bar} {now_week}/{ev.c_end_week}，覆盖。"


def build_weekly_report() -> str:
    now = datetime.now()
    now_week = current_teach_week(now)
    max_week = int(cfg["term"]["max_teach_week"])
    future_weeks = int(cfg["report"].get("future_weeks", 3))
    evs = Event.query.order_by(Event.id.asc()).all()

    curr_lines, future_lines, notes = [], [], []
    for i, ev in enumerate(evs, 1):
        line = f"{i}." + format_event_line(ev, now_week)
        if ev.event_type == "single" and ev.teach_week == now_week:
            curr_lines.append(line)
        elif ev.event_type == "single" and now_week < ev.teach_week <= now_week + future_weeks:
            future_lines.append(line)
        elif ev.event_type in {"recurring", "coverage"}:
            curr_lines.append(line)
            future_lines.append(line)
        if ev.note:
            notes.append(f"{ev.name}：{ev.note}")

    vars_map = {
        "UserNickname": cfg["user"]["user_nickname"],
        "NowTeachWeek": now_week,
        "MaxTeachWeek": max_week,
        "WeekProgressBar": progress_bar(now_week, max_week),
        "FutureWeeks": future_weeks,
        "CurrentWeekEvents": "\\n".join(curr_lines) if curr_lines else "（无）",
        "FutureEvents": "\\n".join(future_lines) if future_lines else "（无）",
        "EventNotes": "\\n".join(notes) if notes else "（如果没有备注将不会在此处列出）",
    }
    return cfg["report"]["template"].format(**vars_map)


def check_and_fire_events():
    now = datetime.now().replace(second=0, microsecond=0)
    for ev in Event.query.all():
        should = False
        if ev.event_type == "single":
            should = event_trigger_time(ev) == now
        elif ev.event_type == "recurring":
            should = should_fire_recurring(ev, now)
        elif ev.event_type == "coverage":
            should = in_coverage(ev, now)
        if not should or not ev.reminder_enable:
            continue
        exists = NotificationLog.query.filter_by(event_id=ev.id, trigger_at=now).first()
        if exists:
            continue
        body = f"事件触发：{ev.name}\\n时间：{now}\\n备注：{ev.note or '无'}"
        if ev.remind_pushdeer:
            send_pushdeer(f"教学事件提醒：{ev.name}", body)
        if ev.remind_email:
            send_graph_email(f"教学事件提醒：{ev.name}", body)
        db.session.add(NotificationLog(event_id=ev.id, trigger_at=now))
    db.session.commit()


def parse_schedule_text(v: str):
    # Mon 12:00
    day_map = {"Mon": 0, "Tue": 1, "Wed": 2, "Thu": 3, "Fri": 4, "Sat": 5, "Sun": 6}
    d, t = v.split()
    hh, mm = [int(x) for x in t.split(":")]
    return day_map[d], hh, mm


def send_weekly_report(schedule_key: str):
    now_week = current_teach_week()
    if ReportLog.query.filter_by(schedule_key=schedule_key, week_index=now_week).first():
        return
    body = build_weekly_report()
    send_pushdeer("教学周报", body)
    send_graph_email("教学周报", body)
    db.session.add(ReportLog(schedule_key=schedule_key, week_index=now_week))
    db.session.commit()


def start_scheduler():
    scheduler.add_job(check_and_fire_events, CronTrigger(second=0), id="event-check", replace_existing=True)
    if cfg["report"].get("enable"):
        for sch in cfg["report"].get("schedules", []):
            wd, hh, mm = parse_schedule_text(sch)
            scheduler.add_job(lambda k=sch: send_weekly_report(k), CronTrigger(day_of_week=wd, hour=hh, minute=mm), id=f"report-{sch}", replace_existing=True)
    scheduler.start()


@app.route("/")
def index():
    now_week = current_teach_week()
    max_week = cfg["term"]["max_teach_week"]
    start = term_start()
    rows = []
    for w in range(1, max_week + 1):
        row = []
        for d in range(1, 8):
            dt = start + timedelta(weeks=w - 1, days=d - 1)
            row.append((dt.month, dt.day))
        rows.append((w, row))
    events = Event.query.order_by(Event.id.desc()).all()
    return render_template_string(
        """
<!doctype html><html><head><meta charset='utf-8'><title>教学周日历</title>
<style>body{font-family:Arial;margin:20px}.grid{border-collapse:collapse}td,th{border:1px solid #b7d0e8;padding:8px;text-align:center}.week{width:90px;background:#f5f8fb}.sun{color:#ff6600}.sat{color:#ff6600}.card{padding:12px;border:1px solid #ddd;margin:8px 0}</style>
</head><body>
<h1>{{term_name}}</h1>
<p>Hi, {{nick}}！现在是第{{now_week}}教学周 {{bar}} {{now_week}}/{{max_week}}</p>
<p><a href='{{url_for("new_event")}}'>新增事件</a> | <a href='{{url_for("settings")}}'>设置</a> | <a href='{{url_for("preview_report")}}'>预览周报</a></p>
<table class='grid'><tr><th>周次</th><th>周一</th><th>周二</th><th>周三</th><th>周四</th><th>周五</th><th class='sat'>周六</th><th class='sun'>周日</th></tr>
{% for w, days in rows %}<tr><td class='week'>第{{w}}教学周</td>{% for m,d in days %}<td>{{d}}<br><small>{{m}}月</small></td>{% endfor %}</tr>{% endfor %}</table>
<h3>事件列表</h3>
{% for e in events %}<div class='card'><b>{{e.name}}</b> ({{e.event_type}}) - <a href='{{url_for("delete_event", eid=e.id)}}'>删除</a><br>{{format_event_line(e, now_week)}}</div>{% else %}<p>暂无事件</p>{% endfor %}
</body></html>
""",
        rows=rows,
        now_week=now_week,
        max_week=max_week,
        term_name=cfg["term"]["name"],
        nick=cfg["user"]["user_nickname"],
        bar=progress_bar(now_week, max_week),
        events=events,
        format_event_line=format_event_line,
    )


@app.route("/report/preview")
def preview_report():
    return f"<pre>{build_weekly_report()}</pre><p><a href='/'>返回</a></p>"


@app.route('/events/new', methods=['GET', 'POST'])
def new_event():
    if request.method == 'POST':
        f = request.form
        ev = Event(
            name=f['name'],
            event_type=f['event_type'],
            note=f.get('note', ''),
            reminder_enable=bool(f.get('reminder_enable')),
            remind_pushdeer=bool(f.get('remind_pushdeer')),
            remind_email=bool(f.get('remind_email')),
        )
        def gi(k, d=0): return int(f.get(k, d) or d)
        ev.teach_week, ev.weekday, ev.hour, ev.minute = gi('teach_week'), gi('weekday'), gi('hour'), gi('minute')
        ev.r_start_week, ev.r_start_weekday, ev.r_start_hour, ev.r_start_minute = gi('r_start_week'), gi('r_start_weekday'), gi('r_start_hour'), gi('r_start_minute')
        ev.r_every_weeks, ev.r_every_days, ev.r_every_hours, ev.r_every_minutes = gi('r_every_weeks'), gi('r_every_days'), gi('r_every_hours'), gi('r_every_minutes')
        ev.c_start_week, ev.c_start_weekday, ev.c_start_hour, ev.c_start_minute = gi('c_start_week'), gi('c_start_weekday'), gi('c_start_hour'), gi('c_start_minute')
        ev.c_end_week, ev.c_end_weekday, ev.c_end_hour, ev.c_end_minute = gi('c_end_week'), gi('c_end_weekday'), gi('c_end_hour'), gi('c_end_minute')
        db.session.add(ev)
        db.session.commit()
        return redirect(url_for('index'))
    return render_template_string("""
    <h2>新增事件</h2><form method='post'>
    名称<input name='name' required><br>类型<select name='event_type'><option value='single'>single</option><option value='recurring'>recurring</option><option value='coverage'>coverage</option></select><br>
    备注<input name='note'><br>
    单次: 第<input name='teach_week' size=3>教学周 周<input name='weekday' size=3>(1-7) <input name='hour' size=3>:<input name='minute' size=3><br>
    循环起点: 第<input name='r_start_week' size=3>周 周<input name='r_start_weekday' size=3> <input name='r_start_hour' size=3>:<input name='r_start_minute' size=3><br>
    循环间隔: <input name='r_every_weeks' size=3>周 <input name='r_every_days' size=3>日 <input name='r_every_hours' size=3>小时 <input name='r_every_minutes' size=3>分钟<br>
    覆盖区间: 第<input name='c_start_week' size=3>周 周<input name='c_start_weekday' size=3> <input name='c_start_hour' size=3>:<input name='c_start_minute' size=3> 到 第<input name='c_end_week' size=3>周 周<input name='c_end_weekday' size=3> <input name='c_end_hour' size=3>:<input name='c_end_minute' size=3><br>
    启用提醒<input type='checkbox' name='reminder_enable'> Pushdeer<input type='checkbox' name='remind_pushdeer'> Email<input type='checkbox' name='remind_email'><br>
    <button type='submit'>保存</button></form><p><a href='/'>返回</a></p>
    """)


@app.route('/events/<int:eid>/delete')
def delete_event(eid):
    ev = Event.query.get_or_404(eid)
    db.session.delete(ev)
    db.session.commit()
    return redirect(url_for('index'))


@app.route('/settings', methods=['GET', 'POST'])
def settings():
    global cfg
    if request.method == 'POST':
        cfg['user']['username'] = request.form['username']
        cfg['user']['user_nickname'] = request.form['user_nickname']
        cfg['term']['name'] = request.form['term_name']
        cfg['term']['start_date'] = request.form['start_date']
        cfg['term']['end_date'] = request.form['end_date']
        cfg['term']['max_teach_week'] = int(request.form['max_teach_week'])
        cfg['report']['future_weeks'] = int(request.form['future_weeks'])
        cfg['report']['schedules'] = [x.strip() for x in request.form['schedules'].split(',') if x.strip()]
        cfg['report']['template'] = request.form['template']
        cfg['notify']['pushdeer']['enable'] = bool(request.form.get('pd_enable'))
        cfg['notify']['pushdeer']['pushkey'] = request.form['pushkey']
        cfg['notify']['microsoft_graph']['enable'] = bool(request.form.get('mg_enable'))
        cfg['notify']['microsoft_graph']['tenant_id'] = request.form['tenant_id']
        cfg['notify']['microsoft_graph']['client_id'] = request.form['client_id']
        cfg['notify']['microsoft_graph']['recipient_email'] = request.form['recipient_email']
        save_config(cfg)
        flash('设置已保存')
        return redirect(url_for('settings'))
    return render_template_string("""
    <h2>系统设置</h2>{% with m=get_flashed_messages() %}{% for x in m %}<p>{{x}}</p>{% endfor %}{% endwith %}
    <form method='post'>
    Username<input name='username' value='{{c.user.username}}'><br>
    UserNickname<input name='user_nickname' value='{{c.user.user_nickname}}'><br>
    学期名<input name='term_name' value='{{c.term.name}}'><br>
    开始日期<input name='start_date' value='{{c.term.start_date}}'><br>
    结束日期<input name='end_date' value='{{c.term.end_date}}'><br>
    最大教学周<input name='max_teach_week' value='{{c.term.max_teach_week}}'><br>
    周报未来周数<input name='future_weeks' value='{{c.report.future_weeks}}'><br>
    周报计划(逗号分隔, 如 Mon 12:00,Tue 18:30)<input name='schedules' value='{{c.report.schedules|join(",")}}' size='60'><br>
    周报模板<textarea name='template' rows='8' cols='80'>{{c.report.template}}</textarea><br>
    Pushdeer启用<input type='checkbox' name='pd_enable' {% if c.notify.pushdeer.enable %}checked{% endif %}> pushkey<input name='pushkey' value='{{c.notify.pushdeer.pushkey}}' size='50'><br>
    Graph启用<input type='checkbox' name='mg_enable' {% if c.notify.microsoft_graph.enable %}checked{% endif %}> tenant<input name='tenant_id' value='{{c.notify.microsoft_graph.tenant_id}}'> client<input name='client_id' value='{{c.notify.microsoft_graph.client_id}}'><br>
    收件人<input name='recipient_email' value='{{c.notify.microsoft_graph.recipient_email}}'><br>
    <button type='submit'>保存</button></form><p><a href='/'>返回</a></p>
    """, c=cfg)


if __name__ == '__main__':
    with app.app_context():
        db.create_all()
    start_scheduler()
    app.run(host=cfg['app']['host'], port=cfg['app']['port'])
