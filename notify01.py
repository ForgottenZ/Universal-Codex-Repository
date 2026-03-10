#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
轮值提醒（PushDeer + 邮件，支持 Microsoft Graph）

支持两种模式：
1) rotation（日轮值，默认）：按“无状态日历推导”计算每天轮值人
   - rotation.start_date + rotation.start_letter (+ 可选 rotation.order)
   - 去重：state.last_notified_date（全局同日去重）

2) weekly（每人每周自动发送）：按每个人的每周计划发送（可全局默认，也可个人覆盖）
   - weekly.enable: true 时启用
   - 每个人可配置 weekday / weekdays（周几）+ time（几点）：
     * 若个人未配置 weekday，则回落到 weekly.day / weekly.weekday
   - 去重：state.weekly_last_sent[letter]（同一“周”只发送一次；周以周一为起始）

其他特性：
- 通过 LOCAL_TZ 指定时区（如 Asia/Shanghai），避免容器 UTC 带来的跨日错位
- 支持多种发送时间格式：900 / 0900 / "09:00"
- 可选 PushDeer 通知
- 可选邮件通知（Microsoft Graph / Outlook），每个值班人可配置 email 地址
- 支持测试模式（命令行参数），可指定轮值人、强制发送、Dry-Run
"""

import os
import sys
import yaml
import requests
import signal
import argparse
import atexit
from email.message import EmailMessage
from datetime import datetime, date, timedelta
from pathlib import Path
from time import sleep

import msal  # pip install msal

# -------------------- 常量 & 配置 --------------------

PUSHDEER_API = "https://api2.pushdeer.com/message/push"
PEOPLE_DEFAULT = list("abcdefgh")  # 默认轮值键（rotation 模式回退用）
DEFAULT_CONFIG = "/data/rotate_config.yaml"
CONFIG_FILE = Path(os.getenv("CONFIG_PATH", DEFAULT_CONFIG))

# MSAL token 缓存文件（设备码登录后会把 token 缓存在这里，后续自动续期）
DEFAULT_MSAL_CACHE = Path(os.getenv("MSAL_CACHE_PATH", "/data/msal_cache.bin"))

# 循环间隔（秒），默认 300=5 分钟，可用环境变量覆盖
INTERVAL_SEC = int(os.getenv("INTERVAL_SEC", "300"))

# 时区：例如 "Asia/Shanghai"
try:
    from zoneinfo import ZoneInfo  # py3.9+
except Exception:  # pragma: no cover
    ZoneInfo = None
LOCAL_TZ = os.getenv("LOCAL_TZ", "").strip()  # 为空则使用系统时区

TITLE = "轮值提醒：请及时查看弘毅网通知"
BODY_TEMPLATE = (
    "你好，\n\n"
    "今日已轮到 {name} 值守，请尽快登录弘毅网查看最新通知与相关事项，及时处理。\n\n"
    "—— 自动提醒"
)

# -------------------- 运行控制 --------------------

_running = True
def _handle_sigterm(signum, frame):
    global _running
    _running = False
signal.signal(signal.SIGTERM, _handle_sigterm)
signal.signal(signal.SIGINT, _handle_sigterm)  # 本地调试 Ctrl+C

def log(msg: str):
    """使用与逻辑判断相同的 now_dt()，确保日志时间与判断时间一致。"""
    print(f"[{now_dt().isoformat(timespec='seconds')}] {msg}", flush=True)

# -------------------- 工具函数 --------------------

def now_dt():
    """返回当前时区感知的 datetime（若配置时区可用）。"""
    if LOCAL_TZ and ZoneInfo:
        try:
            return datetime.now(ZoneInfo(LOCAL_TZ))
        except Exception:
            pass
    return datetime.now()

def today_date() -> date:
    return now_dt().date()

def now_hhmm() -> int:
    n = now_dt()
    return n.hour * 100 + n.minute

def parse_time_hhmm(val, default=900) -> int:
    """
    解析用户配置的发送时间：
    - 900 / 1330 (int)
    - "0900" / "900" (纯数字字符串)
    - "09:00" / "9:00" (带冒号)
    返回整数 HHMM（如 930、900、1735）。
    """
    if isinstance(val, int):
        return max(0, min(2359, val))
    s = str(val or "").strip()
    if not s:
        return default
    if ":" in s:
        try:
            hh, mm = s.split(":", 1)
            return int(hh) * 100 + int(mm)
        except Exception:
            return default
    if s.isdigit():
        # 3~4 位数字均可（如 "900" / "0900"）
        try:
            return int(s)
        except Exception:
            return default
    return default

# -------------------- weekly（每周）辅助：解析周几 & 本周标识 --------------------

_WEEKDAY_MAP = {
    # English
    "mon": 0, "monday": 0,
    "tue": 1, "tues": 1, "tuesday": 1,
    "wed": 2, "wednesday": 2,
    "thu": 3, "thur": 3, "thurs": 3, "thursday": 3,
    "fri": 4, "friday": 4,
    "sat": 5, "saturday": 5,
    "sun": 6, "sunday": 6,
    # Chinese
    "周一": 0, "星期一": 0, "礼拜一": 0,
    "周二": 1, "星期二": 1, "礼拜二": 1,
    "周三": 2, "星期三": 2, "礼拜三": 2,
    "周四": 3, "星期四": 3, "礼拜四": 3,
    "周五": 4, "星期五": 4, "礼拜五": 4,
    "周六": 5, "星期六": 5, "礼拜六": 5,
    "周日": 6, "周天": 6, "星期日": 6, "星期天": 6, "礼拜日": 6, "礼拜天": 6,
}

def parse_weekday_one(val):
    """
    把“周几”解析成 Python weekday（周一=0 ... 周日=6）。
    支持：
      - 0~6（直接使用）
      - 1~7（视为周一=1 ... 周日=7，自动 -1）
      - "Mon"/"周一"/"星期一"/"1" 等
    """
    if val is None:
        return None
    if isinstance(val, bool):
        return None
    if isinstance(val, int):
        if 0 <= val <= 6:
            return val
        if 1 <= val <= 7:
            return val - 1
        return None

    s = str(val or "").strip()
    if not s:
        return None

    if s.isdigit():
        try:
            n = int(s)
        except Exception:
            return None
        if 0 <= n <= 6:
            return n
        if 1 <= n <= 7:
            return n - 1
        return None

    key = s.lower()
    key = key.replace("星期", "周").replace("礼拜", "周")
    if key in _WEEKDAY_MAP:
        return _WEEKDAY_MAP[key]
    if s in _WEEKDAY_MAP:
        return _WEEKDAY_MAP[s]
    return None

def parse_weekdays(val, default=None) -> list:
    """
    解析 weekday/weekdays 配置，返回去重后的 weekday 列表（0~6）。
    val 可为：单值（int/str）或列表。
    """
    if val is None or val == "":
        return list(default or [])
    items = val if isinstance(val, (list, tuple, set)) else [val]
    out = []
    for it in items:
        w = parse_weekday_one(it)
        if w is None:
            continue
        if w not in out:
            out.append(w)
    return out if out else list(default or [])

def week_key(d: date = None) -> str:
    """
    本周标识：使用“本周周一的日期”作为 key（例如 2025-12-15）。
    这样跨年也很直观，并且与本地时区一致。
    """
    d = d or today_date()
    monday = d - timedelta(days=d.weekday())
    return monday.isoformat()

# -------------------- 配置加载/保存 --------------------

def load_config():
    if not CONFIG_FILE.exists():
        log(f"未找到配置文件：{CONFIG_FILE}")
        sys.exit(1)
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except Exception as e:
        log(f"读取配置异常：{e}")
        sys.exit(1)

    # 合理默认
    data.setdefault("record_state", True)

    # state：去重记录
    if not isinstance(data.get("state"), dict):
        data["state"] = {}
    data.setdefault("state", {})
    data["state"].setdefault("weekly_last_sent", {})  # weekly 模式：每人每周去重

    # people：人员配置
    if not isinstance(data.get("people"), dict):
        data["people"] = {}
    data.setdefault("people", {})

    # rotation：日轮值推导
    if not isinstance(data.get("rotation"), dict):
        data["rotation"] = {}
    data.setdefault("rotation", {})

    # weekly：每人每周发送
    if not isinstance(data.get("weekly"), dict):
        data["weekly"] = {}
    data.setdefault("weekly", {})
    data["weekly"].setdefault("enable", False)

    # email：邮件全局配置
    if not isinstance(data.get("email"), dict):
        data["email"] = {}
    data.setdefault("email", {})

    return data

def save_config(cfg):
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        yaml.safe_dump(cfg, f, allow_unicode=True, sort_keys=False)

# -------------------- 模式判断 --------------------

def weekly_enabled(cfg) -> bool:
    return bool((cfg.get("weekly") or {}).get("enable", False))

# -------------------- 轮值计算（rotation：无状态日历推导） --------------------

def rotation_order(cfg):
    """
    支持在配置里通过 rotation.order 自定义轮换序列。
    - 字符串: "abc..." → list("abc...")
    - 列表: ["a", "b", ...]
    非法/重复/空白会被过滤；最终至少回落到 PEOPLE_DEFAULT。
    """
    rot = cfg.get("rotation") or {}
    order = rot.get("order")
    seq = None
    if isinstance(order, str) and order.strip():
        seq = list(order.strip())
    elif isinstance(order, list) and order:
        seq = [str(x).strip() for x in order]
    else:
        seq = PEOPLE_DEFAULT[:]

    seen, out = set(), []
    for k in seq:
        if not k:
            continue
        if k in seen:
            continue
        seen.add(k)
        out.append(k)
    return out or PEOPLE_DEFAULT[:]

def compute_letter_by_date(cfg) -> str:
    """
    根据 rotation.start_date / rotation.start_letter / rotation.order
    和“今天的日期”，直接计算当天轮到的键（无状态）。
    """
    rot = cfg.get("rotation") or {}
    start_date_s = (rot.get("start_date") or "").strip()
    start_letter = (rot.get("start_letter") or "a").strip().lower()

    order = rotation_order(cfg)
    if start_letter not in order:
        start_letter = order[0]

    try:
        base = date.fromisoformat(start_date_s) if start_date_s else today_date()
    except Exception:
        base = today_date()

    days = (today_date() - base).days
    if days < 0:
        days = 0

    idx0 = order.index(start_letter)
    idx = (idx0 + days) % len(order)
    return order[idx]

def current_person(cfg) -> str:
    """当天轮值键（rotation 模式）。"""
    return compute_letter_by_date(cfg)

def person_config(cfg, letter: str) -> dict:
    return cfg.get("people", {}).get(letter, {}) or {}

def person_emails(pconf) -> list:
    """
    从人员配置中解析 email 字段，支持：
    - 字符串："a@b.com"
    - 列表：["a@b.com", "c@d.com"]
    """
    val = pconf.get("email")
    if not val:
        return []
    if isinstance(val, str):
        v = val.strip()
        return [v] if v else []
    if isinstance(val, (list, tuple, set)):
        out = []
        for item in val:
            s = str(item or "").strip()
            if s and s not in out:
                out.append(s)
        return out
    v = str(val).strip()
    return [v] if v else []

# -------------------- 触发判定/去重（rotation：按天） --------------------

def should_send_today(cfg, letter: str):
    pconf = person_config(cfg, letter)
    pushkey = (pconf.get("pushkey") or "").strip()
    emails = person_emails(pconf)
    has_channel = bool(pushkey) or bool(emails)
    enable = bool(pconf.get("enable", False)) and has_channel
    if not enable:
        return False, "未启用，或未配置 pushkey/email"

    target_hhmm = parse_time_hhmm(pconf.get("time", 900), default=900)  # 默认 09:00
    if now_hhmm() < target_hhmm:
        return False, f"未到发送时间 {target_hhmm:04d}"

    if bool(cfg.get("record_state", True)):
        last_sent = (cfg.get("state", {}).get("last_notified_date") or "").strip()
        if last_sent == today_date().isoformat():
            return False, "今天已发送过"
    return True, "满足条件"

def mark_sent_today(cfg):
    if bool(cfg.get("record_state", True)):
        cfg.setdefault("state", {})["last_notified_date"] = today_date().isoformat()
    return cfg

# -------------------- 触发判定/去重（weekly：每人每周） --------------------

def _weekly_default_weekdays(cfg) -> list:
    wcfg = cfg.get("weekly") or {}
    val = (
        wcfg.get("weekday") if "weekday" in wcfg else
        wcfg.get("day") if "day" in wcfg else
        wcfg.get("week_day") if "week_day" in wcfg else
        wcfg.get("weekdays") if "weekdays" in wcfg else
        None
    )
    return parse_weekdays(val, default=[])

def _weekly_default_time(cfg) -> int:
    wcfg = cfg.get("weekly") or {}
    return parse_time_hhmm(wcfg.get("time", 900), default=900)

def person_weekdays(cfg, pconf) -> list:
    """
    获取某人的“周几”计划：
    - 优先使用个人配置：weekday/week_day/weekdays/days
    - 若个人未配，则回落到 weekly.day / weekly.weekday
    """
    val = (
        pconf.get("weekday") if "weekday" in pconf else
        pconf.get("week_day") if "week_day" in pconf else
        pconf.get("weekdays") if "weekdays" in pconf else
        pconf.get("days") if "days" in pconf else
        None
    )
    return parse_weekdays(val, default=_weekly_default_weekdays(cfg))

def person_send_time_hhmm(cfg, pconf) -> int:
    """
    获取某人的发送时间：
    - 优先个人 time
    - 否则回落 weekly.time
    - 否则 09:00
    """
    default_hhmm = _weekly_default_time(cfg)
    return parse_time_hhmm(pconf.get("time"), default=default_hhmm)

def should_send_weekly(cfg, letter: str):
    """
    weekly 模式下：判断某人本周是否需要发送
    """
    pconf = person_config(cfg, letter)
    pushkey = (pconf.get("pushkey") or "").strip()
    emails = person_emails(pconf)
    has_channel = bool(pushkey) or bool(emails)
    enable = bool(pconf.get("enable", False)) and has_channel
    if not enable:
        return False, "未启用，或未配置 pushkey/email"

    wdays = person_weekdays(cfg, pconf)
    if not wdays:
        return False, "weekly 模式：未配置 weekday（且 weekly.day/weekday 未提供默认）"

    today_w = now_dt().weekday()  # 周一=0 ... 周日=6
    if today_w not in wdays:
        return False, "今天不是计划发送日"

    target_hhmm = person_send_time_hhmm(cfg, pconf)
    if now_hhmm() < target_hhmm:
        return False, f"未到发送时间 {target_hhmm:04d}"

    if bool(cfg.get("record_state", True)):
        state = cfg.get("state", {}) or {}
        weekly_map = state.get("weekly_last_sent", {}) or {}
        last_key = str(weekly_map.get(letter) or "").strip()
        if last_key == week_key():
            return False, "本周已发送过"
    return True, "满足条件"

def mark_sent_weekly(cfg, letter: str):
    if bool(cfg.get("record_state", True)):
        st = cfg.setdefault("state", {})
        mp = st.setdefault("weekly_last_sent", {})
        mp[letter] = week_key()
    return cfg

# -------------------- PushDeer 发送 --------------------

def send_pushdeer(pushkey, title, body):
    try:
        r = requests.post(
            PUSHDEER_API,
            data={"pushkey": pushkey, "text": title, "desp": body},
            timeout=10,
        )
        try:
            j = r.json()
        except Exception:
            j = {"status": r.status_code, "text": (r.text or "")[:200]}
        ok = (r.status_code == 200) and isinstance(j, dict) and (j.get("code") in (0, 200))
        return ok, j
    except Exception as e:
        return False, {"error": str(e)}

# -------------------- MSAL / Graph 辅助函数 --------------------

def _load_msal_cache(cache_path: Path):
    cache = msal.SerializableTokenCache()
    if cache_path.exists():
        try:
            cache.deserialize(cache_path.read_text())
        except Exception as e:
            log(f"反序列化 MSAL 缓存失败，将重新登录：{e}")
    return cache

def _save_msal_cache(cache, cache_path: Path):
    if cache.has_state_changed:
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(cache.serialize())
        except Exception as e:
            log(f"保存 MSAL 缓存失败：{e}")

def _build_msal_app(email_cfg):
    client_id = (email_cfg.get("client_id") or os.getenv("GRAPH_CLIENT_ID", "")).strip()
    tenant = (email_cfg.get("tenant") or os.getenv("GRAPH_TENANT", "consumers")).strip()
    authority = (email_cfg.get("authority") or f"https://login.microsoftonline.com/{tenant}").strip()

    if not client_id:
        raise RuntimeError("email.client_id 未配置，无法使用 Microsoft Graph 发送邮件")

    cache_file = email_cfg.get("cache_file") or os.getenv("MSAL_CACHE_PATH", str(DEFAULT_MSAL_CACHE))
    cache_path = Path(cache_file)
    cache = _load_msal_cache(cache_path)

    app = msal.PublicClientApplication(
        client_id=client_id,
        authority=authority,
        token_cache=cache,
    )

    atexit.register(lambda: _save_msal_cache(cache, cache_path))
    return app, cache, cache_path

def acquire_graph_access_token(email_cfg):
    scopes_cfg = email_cfg.get("scopes")
    if scopes_cfg:
        scopes = [str(s).strip() for s in scopes_cfg if str(s).strip()]
    else:
        scopes = ["Mail.Send", "offline_access", "User.Read"]

    app, cache, cache_path = _build_msal_app(email_cfg)

    accounts = app.get_accounts()
    result = None
    if accounts:
        result = app.acquire_token_silent(scopes=scopes, account=accounts[0])

    if not result:
        flow = app.initiate_device_flow(scopes=scopes)
        if "user_code" not in flow:
            raise RuntimeError(f"设备码登录初始化失败：{flow}")
        print(flow["message"], flush=True)
        result = app.acquire_token_by_device_flow(flow)

    if "access_token" not in result:
        raise RuntimeError(f"获取访问令牌失败：{result.get('error')} {result.get('error_description')}")

    _save_msal_cache(cache, cache_path)
    return result["access_token"]

# -------------------- 使用 Graph 发送邮件 --------------------

def send_email_graph(cfg, to_addrs, subject, body):
    email_cfg = cfg.get("email") or {}
    if not email_cfg.get("enable"):
        return False, "email 未启用"

    from_addr = (email_cfg.get("from_addr") or "").strip()

    if not from_addr:
        return False, "邮件配置不完整（from_addr 未配置）"
    if not to_addrs:
        return False, "收件人为空"

    try:
        access_token = acquire_graph_access_token(email_cfg)
    except Exception as e:
        return False, {"error": f"获取 Graph 访问令牌失败：{e}"}

    message = {
        "subject": subject,
        "body": {"contentType": "Text", "content": body},
        "toRecipients": [{"emailAddress": {"address": addr}} for addr in to_addrs],
        "from": {"emailAddress": {"address": from_addr}},
    }
    payload = {"message": message, "saveToSentItems": True}

    try:
        resp = requests.post(
            "https://graph.microsoft.com/v1.0/me/sendMail",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=10,
        )
    except Exception as e:
        return False, {"error": f"调用 Graph API 失败：{e}"}

    if resp.status_code in (200, 202, 204):
        return True, {"to": to_addrs, "status_code": resp.status_code}
    else:
        try:
            err = resp.json()
        except Exception:
            err = resp.text
        return False, {"status_code": resp.status_code, "error": err}

# -------------------- 一次运行 & 主循环 --------------------

def run_once(forced_letter: str = None, force_send: bool = False, dry_run: bool = False):
    """
    - forced_letter: rotation 模式下：指定一个轮值键，优先于 rotation 推导
      weekly 模式下：若指定，则只检查/发送该人
    - force_send: 忽略时间 & 去重判断（仍然会依赖你是否配置了 pushkey/email）
    - dry_run: 仅打印，不发送，不写入 state
    """
    cfg = load_config()
    use_weekly = weekly_enabled(cfg)

    # ========== weekly 模式：遍历每个人 ==========
    if use_weekly:
        people = cfg.get("people", {}) or {}
        letters = [forced_letter] if forced_letter else list(people.keys())

        if not letters:
            log("[weekly] weekly.enable=true，但 people 为空，跳过。")
            return

        for letter in letters:
            pconf = person_config(cfg, letter)
            name = pconf.get("name") or letter

            if force_send:
                orig_need, orig_reason = should_send_weekly(cfg, letter)
                log(f"【测试模式】强制发送：原始判断 need={orig_need}, reason={orig_reason}")
                need_send, reason = True, "测试模式：强制发送"
            else:
                need_send, reason = should_send_weekly(cfg, letter)

            log_prefix = "[DRY-RUN]" if dry_run else ""
            log(f"{log_prefix} [weekly] 目标：{letter}（{name}） | 发送判断：{need_send}（{reason}）")

            if not need_send:
                continue

            body = BODY_TEMPLATE.format(name=name)
            pushkey = (pconf.get("pushkey") or "").strip()
            email_list = person_emails(pconf)

            ok_push = False
            ok_mail = False

            if dry_run:
                log(f"[DRY-RUN] 将向 {letter}（{name}）发送：")
                log(f"[DRY-RUN]   PushDeer: {'是' if pushkey else '否'}")
                log(f"[DRY-RUN]   Email: {email_list or '无'}")
            else:
                if pushkey:
                    ok_push, payload_push = send_pushdeer(pushkey, TITLE, body)
                    log(f"PushDeer 发送结果：{ok_push} | 返回：{payload_push}")
                else:
                    log("未配置 pushkey，跳过 PushDeer 通知。")

                if email_list:
                    ok_mail, payload_mail = send_email_graph(cfg, email_list, TITLE, body)
                    log(f"邮件发送结果：{ok_mail} | 返回：{payload_mail}")
                else:
                    log("未配置 email，跳过邮件通知。")

                if ok_push or ok_mail:
                    cfg = mark_sent_weekly(cfg, letter)
                else:
                    log("⚠️ 所有渠道都推送失败，请检查 pushkey / email / 网络 / PushDeer / Graph 配置。")

        if not dry_run and bool(cfg.get("record_state", True)):
            save_config(cfg)
        return

    # ========== rotation 模式：保持原逻辑 ==========
    letter = forced_letter or current_person(cfg)
    pconf = person_config(cfg, letter)
    name = pconf.get("name") or letter

    if force_send:
        orig_need, orig_reason = should_send_today(cfg, letter)
        log(f"【测试模式】强制发送：原始判断 need={orig_need}, reason={orig_reason}")
        need_send, reason = True, "测试模式：强制发送"
    else:
        need_send, reason = should_send_today(cfg, letter)

    log_prefix = "[DRY-RUN]" if dry_run else ""
    log(f"{log_prefix} [rotation] 当前轮到：{letter}（{name}） | 发送判断：{need_send}（{reason}）")

    if need_send:
        body = BODY_TEMPLATE.format(name=name)

        pushkey = (pconf.get("pushkey") or "").strip()
        email_list = person_emails(pconf)

        ok_push = False
        ok_mail = False

        if dry_run:
            log(f"[DRY-RUN] 将向 {letter}（{name}）发送：")
            log(f"[DRY-RUN]   PushDeer: {'是' if pushkey else '否'}")
            log(f"[DRY-RUN]   Email: {email_list or '无'}")
        else:
            if pushkey:
                ok_push, payload_push = send_pushdeer(pushkey, TITLE, body)
                log(f"PushDeer 发送结果：{ok_push} | 返回：{payload_push}")
            else:
                log("未配置 pushkey，跳过 PushDeer 通知。")

            if email_list:
                ok_mail, payload_mail = send_email_graph(cfg, email_list, TITLE, body)
                log(f"邮件发送结果：{ok_mail} | 返回：{payload_mail}")
            else:
                log("未配置 email，跳过邮件通知。")

            if ok_push or ok_mail:
                cfg = mark_sent_today(cfg)
            else:
                log("⚠️ 所有渠道都推送失败，请检查 pushkey / email / 网络 / PushDeer / Graph 配置。")

    if not dry_run and bool(cfg.get("record_state", True)):
        save_config(cfg)

def sleep_until_next_interval(interval_sec: int):
    """对齐到下一个整间隔（例如 5 分钟的 00/05/10/…）。"""
    now = now_dt()
    epoch = datetime(1970, 1, 1, tzinfo=now.tzinfo) if now.tzinfo else datetime(1970, 1, 1)
    elapsed = int((now - epoch).total_seconds())
    wait = interval_sec - (elapsed % interval_sec)
    if wait <= 0 or wait > interval_sec:
        wait = interval_sec
    return wait

def main_loop():
    try:
        cfg = load_config()
        mode = "weekly" if weekly_enabled(cfg) else "rotation"
    except Exception:
        mode = "unknown"

    log(f"进程启动：模式={mode}；每 {INTERVAL_SEC} 秒检测一次，配置文件：{CONFIG_FILE}；时区：{LOCAL_TZ or '系统默认'}")
    while _running:
        try:
            run_once()
        except Exception as e:
            log(f"运行异常：{e}")
        wait = sleep_until_next_interval(INTERVAL_SEC)
        while _running and wait > 0:
            step = min(wait, 1)
            sleep(step)
            wait -= step
    log("收到退出信号，已停止。")

# -------------------- 参数解析 & 入口 --------------------

def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="轮值提醒（PushDeer + 邮件 via Microsoft Graph）")
    parser.add_argument(
        "--test-letter", "-L",
        help="测试模式：指定一个轮值键（如 a/b/c），视为“今天轮到此人”；weekly 模式下则只检查/发送此人"
    )
    parser.add_argument(
        "--force-send", "-F",
        action="store_true",
        help="测试模式：强制发送一次，忽略时间与去重判断"
    )
    parser.add_argument(
        "--dry-run", "-n",
        action="store_true",
        help="测试模式：仅打印将要发送的内容，不实际发送"
    )
    parser.add_argument(
        "--one-shot",
        action="store_true",
        help="只执行一次检查（不进入常驻循环），等价于环境变量 ONE_SHOT=1"
    )
    return parser.parse_args(argv)

if __name__ == "__main__":
    args = parse_args()

    if args.one_shot or args.test_letter or args.force_send or args.dry_run or os.getenv("ONE_SHOT", "0") == "1":
        run_once(
            forced_letter=(args.test_letter.strip().lower() if args.test_letter else None),
            force_send=bool(args.force_send),
            dry_run=bool(args.dry_run),
        )
    else:
        main_loop()
