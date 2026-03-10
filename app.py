#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, time
from functools import wraps
from pathlib import Path

import msal
import requests
import yaml
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from flask import Flask, flash, redirect, render_template, request, session, url_for
from flask_sqlalchemy import SQLAlchemy

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.yaml"
LOG_DIR = BASE_DIR / "logs"

DEFAULT_CONFIG = {
    "app": {"secret_key": "replace-this-secret"},
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


class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(64), unique=True, nullable=False)
    password = db.Column(db.String(128), nullable=False)
    nickname = db.Column(db.String(64), nullable=False)
    is_admin = db.Column(db.Boolean, default=False)

    push_enabled = db.Column(db.Boolean, default=False)
    pushkey = db.Column(db.String(256), default="")

    email_enabled = db.Column(db.Boolean, default=False)
    tenant_id = db.Column(db.String(128), default="common")
    client_id = db.Column(db.String(128), default="")
    client_secret = db.Column(db.String(256), default="")
    sender_email = db.Column(db.String(256), default="")

    weekly_enabled = db.Column(db.Boolean, default=True)
    weekly_future_weeks = db.Column(db.Integer, default=3)
    weekly_schedule_weekday = db.Column(db.String(16), default="mon")
    weekly_schedule_time = db.Column(db.String(16), default="12:00")
    weekly_template = db.Column(
        db.Text,
        default="Hi, {UserNickname}!\\n现在是第{NowTeachWeek}教学周！{ProgressBar} {NowTeachWeek}/{MaxTeachWeek}\\n您本周的事件有：\\n{CurrentWeekEvents}\\n您未来{FutureWeeks}周的事件有：\\n{FutureEvents}\\n详细说明：\\n{Notes}",
    )


class Event(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    name = db.Column(db.String(128), nullable=False)
    event_type = db.Column(db.String(20), nullable=False, default="one_time")
    note = db.Column(db.Text, default="")

    start_week = db.Column(db.Integer, nullable=False)
    start_weekday = db.Column(db.Integer, nullable=False)
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
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    kind = db.Column(db.String(32), nullable=False)
    event_id = db.Column(db.Integer, nullable=True)
    unique_key = db.Column(db.String(128), unique=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


@dataclass
class TeachTime:
    week: int
    weekday: int
    hour: int
    minute: int


def current_user() -> User | None:
    uid = session.get("user_id")
    if not uid:
        return None
    return db.session.get(User, uid)


def login_required(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        uid = session.get("user_id")
        if not uid:
            return redirect(url_for("login"))
        # 修复会话残留导致 user=None 的崩溃：比如用户被管理员删除后，旧 session 仍存在
        if db.session.get(User, uid) is None:
            session.clear()
            flash("登录状态已失效，请重新登录")
            return redirect(url_for("login"))
        return func(*args, **kwargs)

    return wrapper


def admin_required(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        user = current_user()
        if not user or not user.is_admin:
            flash("仅管理员可执行该操作")
            return redirect(url_for("index"))
        return func(*args, **kwargs)

    return wrapper


def has_admin() -> bool:
    return User.query.filter_by(is_admin=True).first() is not None


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
    delta = (dt.date() - s).days
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
        interval = (
            (event.repeat_weeks or 0) * 7 * 24 * 60
            + (event.repeat_days or 0) * 24 * 60
            + (event.repeat_hours or 0) * 60
            + (event.repeat_minutes or 0)
        )
        return interval > 0 and diff % interval == 0
    return False


def progress_bar(current: int, maximum: int) -> str:
    ratio = 0 if maximum <= 0 else min(max(current / maximum, 0), 1)
    full = round(ratio * 10)
    return "[" + "■" * full + "□" * (10 - full) + "]"


def log(message: str, level: str = "INFO"):
    now = datetime.now()
    line = f"[{now.isoformat(timespec='seconds')}] [{level}] {message}"
    print(line, flush=True)
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        logfile = LOG_DIR / f"app-{now.strftime('%Y%m%d')}.log"
        with logfile.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def send_pushdeer(user: User, text: str):
    if not (user.push_enabled and user.pushkey):
        return
    try:
        r = requests.get(
            "https://api2.pushdeer.com/message/push",
            params={"pushkey": user.pushkey, "text": "教学周提醒", "desp": text},
            timeout=10,
        )
        if r.status_code != 200:
            log(f"Pushdeer发送失败 user={user.username} status={r.status_code} body={(r.text or '')[:300]}", "WARN")
        else:
            log(f"Pushdeer发送成功 user={user.username}")
    except Exception as exc:
        log(f"Pushdeer异常 user={user.username} error={exc}", "ERROR")


def _normalize_scopes(scopes):
    vals = scopes or []
    out = []
    # MSAL 保留 scope，不能传入 initiate_device_flow
    reserved = {"openid", "profile", "offline_access"}
    for sc in vals:
        t = str(sc).strip()
        if not t:
            continue
        low = t.lower()
        tail = low.split("/")[-1]
        if low in reserved or tail in reserved:
            log(f"忽略保留scope: {t}", "WARN")
            continue
        if "://" in t:
            out.append(t)
        else:
            out.append(f"https://graph.microsoft.com/{t}")

    # 去重
    uniq = []
    for sc in out:
        if sc not in uniq:
            uniq.append(sc)
    return uniq


def _resolve_email_profile(user: User) -> dict:
    cfg = load_config().get("email", {}) or {}
    enable = bool(cfg.get("enable", user.email_enabled))
    auth_type = str(cfg.get("auth_type", "oauth2")).strip().lower()

    username = (cfg.get("username") or user.sender_email or "").strip()
    from_addr = (cfg.get("from_addr") or username).strip()

    tenant_id = (cfg.get("tenant_id") or user.tenant_id or "consumers").strip()
    authority = (cfg.get("authority") or f"https://login.microsoftonline.com/{tenant_id}").strip()
    client_id = (cfg.get("client_id") or user.client_id or "").strip()
    client_secret = (cfg.get("client_secret") or user.client_secret or "").strip()

    configured_scopes = cfg.get("scopes")
    if configured_scopes:
        scopes = _normalize_scopes(configured_scopes)
    elif client_secret:
        scopes = ["https://graph.microsoft.com/.default"]
    else:
        scopes = ["https://graph.microsoft.com/Mail.Send", "https://graph.microsoft.com/User.Read"]

    return {
        "enable": enable,
        "auth_type": auth_type,
        "username": username,
        "from_addr": from_addr,
        "authority": authority,
        "client_id": client_id,
        "client_secret": client_secret,
        "scopes": scopes,
    }


def _acquire_graph_token(user: User, profile: dict) -> tuple[str | None, bool]:
    authority = profile["authority"]
    scopes = profile["scopes"]
    client_id = profile["client_id"]
    client_secret = profile["client_secret"]

    if not client_id:
        log(f"Graph配置缺失 client_id user={user.username}", "ERROR")
        return None, True

    # auth_type=oath2 且带 client_secret 时优先应用凭据
    if client_secret:
        app_client = msal.ConfidentialClientApplication(
            client_id=client_id,
            client_credential=client_secret,
            authority=authority,
        )
        token = app_client.acquire_token_for_client(scopes=["https://graph.microsoft.com/.default"])
        if "access_token" in token:
            return token.get("access_token"), False
        log(f"Graph应用凭据取token失败 user={user.username} detail={token.get('error_description') or token}", "WARN")

    # 参考 notify01.py：设备码 + token cache
    safe_name = "".join(ch for ch in (profile.get("username") or user.username) if ch.isalnum() or ch in ('-','_','.'))
    cache_file = BASE_DIR / f"msal_cache_{safe_name}.bin"
    cache = msal.SerializableTokenCache()
    if cache_file.exists():
        try:
            cache.deserialize(cache_file.read_text(encoding="utf-8"))
        except Exception as exc:
            log(f"MSAL缓存读取失败 user={user.username} error={exc}", "WARN")

    scopes = _normalize_scopes(scopes)
    if not scopes:
        log(f"Graph配置缺失有效scopes user={user.username}", "ERROR")
        return None, True

    pca = msal.PublicClientApplication(client_id=client_id, authority=authority, token_cache=cache)
    account = None
    accounts = pca.get_accounts(username=profile.get("username")) or pca.get_accounts()
    if accounts:
        account = accounts[0]
    token = pca.acquire_token_silent(scopes, account=account)
    if not token:
        flow = pca.initiate_device_flow(scopes=scopes)
        if "user_code" not in flow:
            log(f"设备码流程初始化失败 user={user.username} detail={flow}", "ERROR")
            return None, True
        log(f"请为用户 {user.username} 完成Graph设备码登录：{flow.get('message')}", "WARN")
        token = pca.acquire_token_by_device_flow(flow)

    if cache.has_state_changed:
        try:
            cache_file.write_text(cache.serialize(), encoding="utf-8")
        except Exception as exc:
            log(f"MSAL缓存写入失败 user={user.username} error={exc}", "WARN")

    if "access_token" in token:
        return token.get("access_token"), True
    log(f"Graph设备码取token失败 user={user.username} detail={token.get('error_description') or token}", "ERROR")
    return None, True


def send_graph_email(user: User, text: str):
    profile = _resolve_email_profile(user)
    if not profile["enable"]:
        log(f"Graph邮件跳过 user={user.username}（email.enable=false）", "WARN")
        return

    sender = profile.get("from_addr") or profile.get("username")
    if not sender:
        log(f"Graph邮件跳过 user={user.username}（缺少 from_addr/username）", "WARN")
        return

    token, is_delegated = _acquire_graph_token(user, profile)
    if not token:
        return

    payload = {
        "message": {
            "subject": "教学周提醒",
            "body": {"contentType": "Text", "content": text},
            "toRecipients": [{"emailAddress": {"address": sender}}],
            "from": {"emailAddress": {"address": sender}},
        },
        "saveToSentItems": True,
    }

    url = "https://graph.microsoft.com/v1.0/me/sendMail" if is_delegated else f"https://graph.microsoft.com/v1.0/users/{sender}/sendMail"
    try:
        r = requests.post(
            url,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            data=json.dumps(payload),
            timeout=20,
        )
        if r.status_code not in (200, 202):
            log(f"Graph邮件发送失败 user={user.username} status={r.status_code} body={(r.text or '')[:500]}", "ERROR")
        else:
            log(f"Graph邮件发送成功 user={user.username} mode={'delegated' if is_delegated else 'app'} sender={sender}")
    except Exception as exc:
        log(f"Graph邮件异常 user={user.username} error={exc}", "ERROR")


def send_notification(user: User, content: str, use_push: bool, use_email: bool):
    log(f"触发通知 user={user.username} push={use_push} email={use_email}")
    if use_push:
        send_pushdeer(user, content)
    if use_email:
        send_graph_email(user, content)


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
    span = max((event.end_week or event.start_week) - event.start_week + 1, 1)
    done = max(now_week - event.start_week + 1, 0)
    return f"{event.name}：第{event.start_week}教学周-第{event.end_week}教学周{progress_bar(done, span)} {done}/{span}，覆盖。"


def render_weekly_report(user: User) -> str:
    current_week = to_teach_time(datetime.now()).week
    future_weeks = int(user.weekly_future_weeks or 3)
    this_week, future, notes = [], [], []
    for e in Event.query.filter_by(user_id=user.id).all():
        if e.note:
            notes.append(f"{e.name}：{e.note}")
        if e.event_type == "range":
            if e.start_week <= current_week <= (e.end_week or e.start_week):
                this_week.append(format_event_brief(e, current_week))
            elif current_week < e.start_week <= current_week + future_weeks:
                future.append(format_event_brief(e, current_week))
            continue
        if e.start_week == current_week or e.event_type == "recurring":
            this_week.append(format_event_brief(e, current_week))
        if (current_week < e.start_week <= current_week + future_weeks) or e.event_type == "recurring":
            future.append(format_event_brief(e, current_week))

    vars_map = {
        "UserNickname": user.nickname,
        "NowTeachWeek": current_week,
        "MaxTeachWeek": max_teach_week(),
        "ProgressBar": progress_bar(current_week, max_teach_week()),
        "FutureWeeks": future_weeks,
        "CurrentWeekEvents": "\\n".join(f"{i+1}.{x}" for i, x in enumerate(this_week)) or "无",
        "FutureEvents": "\\n".join(f"{i+1}.{x}" for i, x in enumerate(future)) or "无",
        "Notes": "\\n".join(notes) or "（如果没有备注将不会在此处列出）",
    }
    return (user.weekly_template or "").format_map(vars_map)


def notify_due_events():
    with app.app_context():
        now = datetime.now().replace(second=0, microsecond=0)
        log(f"开始扫描事件 now={now}")
        for user in User.query.all():
            for event in Event.query.filter_by(user_id=user.id).all():
                if not event_matches_now(event, now):
                    continue
                key = f"event-{user.id}-{event.id}-{now.strftime('%Y%m%d%H%M')}"
                if NotifyLog.query.filter_by(unique_key=key).first():
                    continue
                text = event.remind_text or f"事件提醒：{event.name}"
                send_notification(user, text, event.remind_push, event.remind_email)
                db.session.add(NotifyLog(user_id=user.id, kind="event", event_id=event.id, unique_key=key))
                db.session.commit()
                log(f"事件提醒已发送 user={user.username} event={event.name}")


def weekly_report_job(user_id: int):
    with app.app_context():
        user = db.session.get(User, user_id)
        if not user or not user.weekly_enabled:
            return
        now = datetime.now()
        key = f"weekly-{user.id}-{now.strftime('%Y%W')}"
        if NotifyLog.query.filter_by(unique_key=key).first():
            return
        report = render_weekly_report(user)
        log(f"发送每周报告 user={user.username}")
        send_notification(user, report, True, True)
        db.session.add(NotifyLog(user_id=user.id, kind="weekly_report", unique_key=key))
        db.session.commit()


def setup_scheduler():
    scheduler = BackgroundScheduler()
    scheduler.add_job(notify_due_events, "interval", minutes=1, id="event_scanner")
    with app.app_context():
        for user in User.query.all():
            if not user.weekly_enabled:
                continue
            weekday = (user.weekly_schedule_weekday or "mon").lower()[:3]
            hh, mm = parse_hhmm(user.weekly_schedule_time or "12:00")
            trigger = CronTrigger(day_of_week=weekday, hour=hh, minute=mm)
            scheduler.add_job(lambda uid=user.id: weekly_report_job(uid), trigger, id=f"weekly_{user.id}", replace_existing=True)
    scheduler.start()
    log("调度器已启动")


@app.route("/setup-admin", methods=["GET", "POST"])
def setup_admin():
    if has_admin():
        return redirect(url_for("login"))
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        nickname = request.form.get("nickname", "").strip()
        password = request.form.get("password", "")
        if not username or not nickname or not password:
            flash("请填写完整管理员信息")
            return render_template("setup_admin.html")
        admin = User(username=username, nickname=nickname, password=password, is_admin=True)
        db.session.add(admin)
        db.session.commit()
        flash("管理员创建成功，请登录")
        return redirect(url_for("login"))
    return render_template("setup_admin.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if not has_admin():
        return redirect(url_for("setup_admin"))
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        user = User.query.filter_by(username=username, password=password).first()
        if user:
            session["user_id"] = user.id
            flash("登录成功")
            return redirect(url_for("index"))
        flash("用户名或密码错误")
    return render_template("login.html")


@app.post("/logout")
def logout():
    session.clear()
    flash("已退出登录")
    return redirect(url_for("login"))


@app.route("/")
@login_required
def index():
    cfg = load_config()
    user = current_user()
    if user is None:
        session.clear()
        flash("登录状态已失效，请重新登录")
        return redirect(url_for("login"))
    events = Event.query.filter_by(user_id=user.id).order_by(Event.start_week, Event.start_weekday, Event.start_hour).all()
    s, _ = term_dates()
    cal = []
    for w in range(1, max_teach_week() + 1):
        row = []
        for d in range(1, 8):
            dt = s + timedelta(days=(w - 1) * 7 + d - 1)
            row.append({"date": dt.day, "month": dt.month})
        cal.append({"week": w, "days": row})
    users = User.query.order_by(User.id).all() if user.is_admin else []
    return render_template("index.html", cfg=cfg, user=user, users=users, events=events, cal=cal)


@app.post("/config")
@login_required
def save_config():
    cfg = load_config()
    user = current_user()

    cfg["teaching_calendar"]["term_name"] = request.form["term_name"]
    cfg["teaching_calendar"]["start_date"] = request.form["start_date"]
    cfg["teaching_calendar"]["end_date"] = request.form["end_date"]
    CONFIG_PATH.write_text(yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False), encoding="utf-8")

    user.nickname = request.form["nickname"]
    user.push_enabled = request.form.get("push_enabled") == "on"
    user.pushkey = request.form.get("pushkey", "")
    user.email_enabled = request.form.get("email_enabled") == "on"
    user.tenant_id = request.form.get("tenant_id", "common")
    user.client_id = request.form.get("client_id", "")
    user.client_secret = request.form.get("client_secret", "")
    user.sender_email = request.form.get("sender_email", "")
    user.weekly_template = request.form.get("weekly_template", user.weekly_template)
    user.weekly_future_weeks = int(request.form.get("future_weeks", user.weekly_future_weeks) or 3)
    user.weekly_schedule_weekday = request.form.get("weekly_weekday", user.weekly_schedule_weekday or "mon")
    user.weekly_schedule_time = request.form.get("weekly_time", user.weekly_schedule_time or "12:00")
    user.weekly_enabled = request.form.get("weekly_enabled") == "on"

    db.session.commit()
    flash("配置已保存（周报调度重启后生效）")
    return redirect(url_for("index"))


@app.post("/admin/users")
@login_required
@admin_required
def create_user():
    username = request.form.get("username", "").strip()
    nickname = request.form.get("nickname", "").strip()
    password = request.form.get("password", "")
    is_admin = request.form.get("is_admin") == "on"
    if not username or not nickname or not password:
        flash("用户名/称呼/密码不能为空")
        return redirect(url_for("index"))
    if User.query.filter_by(username=username).first():
        flash("用户名已存在")
        return redirect(url_for("index"))
    db.session.add(User(username=username, nickname=nickname, password=password, is_admin=is_admin))
    db.session.commit()
    flash("用户创建成功")
    return redirect(url_for("index"))


@app.post("/admin/users/<int:user_id>/delete")
@login_required
@admin_required
def delete_user(user_id):
    me = current_user()
    target = db.session.get(User, user_id)
    if not target:
        flash("用户不存在")
        return redirect(url_for("index"))
    if target.id == me.id:
        flash("不能删除当前登录管理员")
        return redirect(url_for("index"))
    if target.is_admin and User.query.filter_by(is_admin=True).count() <= 1:
        flash("至少保留一个管理员")
        return redirect(url_for("index"))

    Event.query.filter_by(user_id=target.id).delete()
    NotifyLog.query.filter_by(user_id=target.id).delete()
    db.session.delete(target)
    db.session.commit()
    flash("用户删除成功")
    return redirect(url_for("index"))


@app.post("/events")
@login_required
def create_event():
    user = current_user()
    event_type = request.form["event_type"]
    e = Event(
        user_id=user.id,
        name=request.form["name"],
        event_type=event_type,
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
    if event_type == "recurring" and not any([e.repeat_weeks, e.repeat_days, e.repeat_hours, e.repeat_minutes]):
        flash("循环事件必须设置至少一个循环间隔")
        return redirect(url_for("index"))
    db.session.add(e)
    db.session.commit()
    flash("事件已创建")
    return redirect(url_for("index"))


@app.post("/events/<int:event_id>/delete")
@login_required
def delete_event(event_id):
    user = current_user()
    target = Event.query.filter_by(id=event_id, user_id=user.id).first()
    if not target:
        flash("事件不存在或无权限")
        return redirect(url_for("index"))
    db.session.delete(target)
    db.session.commit()
    flash("事件已删除")
    return redirect(url_for("index"))


@app.post("/test-notify")
@login_required
def test_notify():
    user = current_user()
    content = request.form.get("content", "这是一条测试通知。")
    use_push = request.form.get("use_push") == "on"
    use_email = request.form.get("use_email") == "on"
    send_notification(user, content, use_push, use_email)
    flash("测试通知已触发，请检查对应渠道。")
    return redirect(url_for("index"))


with app.app_context():
    db.create_all()

setup_scheduler()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
