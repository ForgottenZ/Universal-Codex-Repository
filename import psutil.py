import argparse
import ctypes
import datetime
import logging
import os
import shutil
import subprocess
import sys
import time
from collections import deque
from logging.handlers import RotatingFileHandler
from typing import List, Optional, Tuple

import psutil
import requests
import winreg

# ================= 配置区 =================
# 支持多个 key：可填列表，或逗号分隔字符串
PUSH_KEY = "你的PushDeer_Key_填在这里"
TARGET_PROCESS = "gameviewerserver.exe"

# 你原来的 marker 文件路径（如果这里不可写，会自动回退到 APPDATA）
DEFAULT_MARKER_FILE = r"D:\uuyc-notify.luobo"

TRAFFIC_HIGH_THRESHOLD_KB = 100   # KB/s，超过认为“连入开始”
ZERO_THRESHOLD = 30               # 连续“近似0流量”的秒数，认为“连入结束”
ZERO_KB_EPS = 1.0                 # <= 1KB/s 都当作 0（防止极小写入打断计数）
TIMEOUT_MINUTES = 20              # 空闲超时提醒（分钟）
SHUTDOWN_TRIGGER_TIMEOUT_HITS = 6  # 连续超时提醒次数达到该值后，触发关机流程（6 * 20min = 120min）
SHUTDOWN_COMMAND_SECONDS = 120       # 达成阈值后直接执行 shutdown -s -t 120

DEFAULT_STARTUP_CHECK_SECONDS = 60  # 启动时先监听多少秒，用于判断初始是否已在连接中

# 提醒开关配置（True=启用，False=禁用）
# 可配置项：
# - session_end: 连入流程结束提醒
# - timeout_warning: 设备超时警告提醒
# - shutdown_warning: 关机提醒（含弹窗与 push）
REMINDER_ENABLED = {
    "session_end": True,
    "timeout_warning": True,
    "shutdown_warning": True,
}

DISABLE_FILE_PATH = r"D:\uuyc.disable"  # 存在则进入离线模式
SCREENSHOT_SAVE_DIR = r"D:\uuyc-snapshots"
SCREENSHOT_INTERVAL_MINUTES = 20
SCREENSHOT_RETENTION_DAYS = 90

RUN_VALUE_NAME = "UUYC_Monitor"   # 注册表启动项名称
# =========================================


def get_app_dir() -> str:
    """放日志/回退 marker 的目录（一般可写）"""
    base = (
        os.environ.get("APPDATA")
        or os.environ.get("LOCALAPPDATA")
        or os.path.dirname(os.path.abspath(sys.argv[0]))
    )
    path = os.path.join(base, "uuyc-monitor")
    os.makedirs(path, exist_ok=True)
    return path


def is_reminder_enabled(kind: str) -> bool:
    return bool(REMINDER_ENABLED.get(kind, True))


def is_offline_mode() -> bool:
    """离线模式：disable 文件存在时，不执行提醒/监控相关逻辑，仅保活与截图。"""
    return os.path.exists(DISABLE_FILE_PATH)


APP_DIR = get_app_dir()
LOG_FILE = os.path.join(APP_DIR, "uuyc-monitor.log")
FALLBACK_MARKER_FILE = os.path.join(APP_DIR, "uuyc-notify.luobo")


def get_script_path() -> Tuple[str, bool]:
    """返回 (路径, 是否为打包exe)"""
    if getattr(sys, "frozen", False):
        return os.path.abspath(sys.executable), True
    return os.path.abspath(sys.argv[0]), False


def get_run_dir() -> str:
    """脚本/程序运行文件所在目录"""
    script_path, _ = get_script_path()
    return os.path.dirname(script_path)


def get_debug_console_redirect_log() -> str:
    """debugnet + debug 同时开启时，原控制台日志重定向到这里"""
    return os.path.join(get_run_dir(), "uuyc-monitor.log")


def positive_int(value: str) -> int:
    try:
        n = int(value)
    except ValueError as e:
        raise argparse.ArgumentTypeError("必须是正整数") from e
    if n <= 0:
        raise argparse.ArgumentTypeError("必须大于 0")
    return n


def setup_logging(debug: bool, debugnet_seconds: Optional[int]) -> logging.Logger:
    logger = logging.getLogger("uuyc-monitor")
    logger.setLevel(logging.DEBUG if debug else logging.INFO)

    # 避免重复添加 handler（比如被重复 import 时）
    if logger.handlers:
        return logger

    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

    # 主日志：仍然写到 APP_DIR 下
    fh = RotatingFileHandler(
        LOG_FILE,
        maxBytes=2 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8"
    )
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    # debug 开启时：
    # - 若没开 debugnet：照旧输出到控制台
    # - 若同时开了 debugnet：原本该进控制台的日志，改写到运行目录/uuyc-monitor.log
    if debug:
        if debugnet_seconds is not None:
            redirect_log = get_debug_console_redirect_log()
            try:
                rh = RotatingFileHandler(
                    redirect_log,
                    maxBytes=2 * 1024 * 1024,
                    backupCount=1,
                    encoding="utf-8"
                )
                rh.setFormatter(fmt)
                logger.addHandler(rh)
            except Exception:
                # 兜底：如果运行目录不可写，就退回控制台
                sh = logging.StreamHandler(sys.stdout)
                sh.setFormatter(fmt)
                logger.addHandler(sh)
        else:
            sh = logging.StreamHandler(sys.stdout)
            sh.setFormatter(fmt)
            logger.addHandler(sh)

    return logger


def resolve_marker_file(logger: logging.Logger) -> str:
    """优先用 DEFAULT_MARKER_FILE，不可写则回退到 APPDATA"""
    path = DEFAULT_MARKER_FILE
    try:
        parent = os.path.dirname(os.path.abspath(path)) or "."
        os.makedirs(parent, exist_ok=True)
        with open(path, "a", encoding="utf-8"):
            pass
        return path
    except Exception as e:
        logger.warning(
            "MARKER_FILE '%s' 不可写，已回退到 '%s'（原因：%s）",
            path, FALLBACK_MARKER_FILE, e
        )
        return FALLBACK_MARKER_FILE


def guess_pythonw(python_exe: str) -> str:
    """给定 python.exe，尽量找到同目录 pythonw.exe（无窗口）"""
    base = os.path.basename(python_exe).lower()
    if base == "pythonw.exe":
        return python_exe
    if base == "python.exe":
        cand = os.path.join(os.path.dirname(python_exe), "pythonw.exe")
        if os.path.exists(cand):
            return cand
    cand = os.path.join(os.path.dirname(python_exe), "pythonw.exe")
    if os.path.exists(cand):
        return cand
    return python_exe


def guess_python_console(python_exe: str) -> str:
    """给 bat 调试用：尽量用 python.exe 以便有控制台"""
    base = os.path.basename(python_exe).lower()
    if base == "pythonw.exe":
        cand = os.path.join(os.path.dirname(python_exe), "python.exe")
        if os.path.exists(cand):
            return cand
    return python_exe


def write_debug_bat(script_path: str, python_exe: str, script_dir: str, logger: logging.Logger) -> None:
    """
    生成调试用 bat：
      - chcp 65001
      - pause
    用于手动双击调试（会有窗口，这是故意的）。
    """
    bat_path = os.path.join(script_dir, "uuyc-monitor.bat")
    python_console = guess_python_console(python_exe)

    content = (
        "@echo off\n"
        "chcp 65001 >nul\n"
        "set PYTHONUTF8=1\n"
        "set PYTHONIOENCODING=utf-8\n"
        f"\"{python_console}\" -u \"{script_path}\" --debug\n"
        "pause\n"
    )

    if os.path.exists(bat_path):
        logger.info("调试 BAT 已存在，跳过重写：%s", bat_path)
        return

    try:
        with open(bat_path, "w", encoding="utf-8") as f:
            f.write(content)
        logger.info("已生成调试 BAT：%s", bat_path)
    except Exception as e:
        logger.error("生成 BAT 失败：%s", e)


def send_pushdeer(text: str, desp: str = "", logger: Optional[logging.Logger] = None) -> None:
    keys = normalize_push_keys(PUSH_KEY)
    if not keys:
        if logger:
            logger.warning("PushDeer Key 未设置，跳过发送：%s", text)
        return

    url = "https://api2.pushdeer.com/message/push"
    for key in keys:
        try:
            r = requests.get(
                url,
                params={"pushkey": key, "text": text, "desp": desp},
                timeout=5
            )
            if logger:
                logger.info("[通知] 已发送：%s（key=%s, HTTP %s）", text, key[:6] + "***", r.status_code)
        except Exception as e:
            if logger:
                logger.error("[错误] 通知发送失败（key=%s）：%s", key[:6] + "***", e)


def normalize_push_keys(push_key_value: object) -> List[str]:
    """支持 str/list/tuple/set；str 可用逗号、分号、换行分隔多个 key。"""
    if push_key_value is None:
        return []

    if isinstance(push_key_value, str):
        raw_items = (
            push_key_value
            .replace("；", ",")
            .replace(";", ",")
            .replace("\n", ",")
            .split(",")
        )
    elif isinstance(push_key_value, (list, tuple, set)):
        raw_items = [str(x) for x in push_key_value]
    else:
        raw_items = [str(push_key_value)]

    keys: List[str] = []
    for item in raw_items:
        key = item.strip()
        if not key:
            continue
        if "填在这里" in key:
            continue
        keys.append(key)

    # 去重并保持顺序
    deduped: List[str] = []
    seen = set()
    for key in keys:
        if key.lower() in seen:
            continue
        seen.add(key.lower())
        deduped.append(key)
    return deduped


def show_shutdown_warning_popup(logger: logging.Logger) -> None:
    msg = (
        "设备已连续空闲超过 2 小时（6 次 20 分钟）。\n"
        f"系统已执行 shutdown -s -t {SHUTDOWN_COMMAND_SECONDS}。"
    )
    title = "UUYC Monitor 关机提醒"
    try:
        ctypes.windll.user32.MessageBoxW(0, msg, title, 0x30)  # MB_ICONWARNING
        logger.warning("已显示关机提醒弹窗。")
    except Exception as e:
        logger.error("显示关机提醒弹窗失败：%s", e)


def schedule_forced_shutdown(logger: logging.Logger) -> None:
    """直接下发 Windows 关机倒计时命令（不在脚本里 sleep）。"""
    cmd = f"shutdown -s -t {SHUTDOWN_COMMAND_SECONDS}"
    rc = os.system(cmd)
    if rc == 0:
        logger.warning("已执行关机命令：%s", cmd)
    else:
        logger.error("关机命令执行失败（%s），返回码：%s", cmd, rc)


def parse_debug_switches(argv: List[str]) -> set:
    """
    支持 --debug-xxxx 风格自定义测试开关，例如：
      --debug-shutdown
    """
    switches = set()
    for arg in argv:
        if not arg.startswith("--debug-"):
            continue
        name = arg[len("--debug-"):].strip().lower()
        if name:
            switches.add(name)
    return switches


def ensure_screenshot_retention(root_dir: str, keep_days: int, logger: logging.Logger) -> None:
    """
    只保留最近 keep_days 个“日期文件夹”（yyyy-mm-dd）。
    若超过则按日期从旧到新删除，直到数量 <= keep_days。
    """
    try:
        os.makedirs(root_dir, exist_ok=True)
        day_dirs = []
        for name in os.listdir(root_dir):
            path = os.path.join(root_dir, name)
            if os.path.isdir(path):
                day_dirs.append((name, path))

        day_dirs.sort(key=lambda x: x[0])  # yyyy-mm-dd 字符串可直接排序
        while len(day_dirs) > keep_days:
            day_name, day_path = day_dirs.pop(0)
            try:
                shutil.rmtree(day_path)
                logger.info("截图留存清理：已删除最早目录 %s", day_name)
            except Exception as e:
                logger.error("截图留存清理失败（%s）：%s", day_name, e)
                break
    except Exception as e:
        logger.error("截图留存检查失败：%s", e)


def capture_fullscreen_to_jpg(root_dir: str, logger: logging.Logger) -> None:
    """
    全屏截图保存为：
      指定目录/yyyy-mm-dd/HH-MM-SS.jpg
    """
    now = datetime.datetime.now()
    day_dir = os.path.join(root_dir, now.strftime("%Y-%m-%d"))
    os.makedirs(day_dir, exist_ok=True)
    target_file = os.path.join(day_dir, now.strftime("%H-%M-%S.jpg"))

    # 使用 PowerShell + .NET 截图，避免引入第三方依赖
    ps_script = rf"""
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
$bounds = [System.Windows.Forms.Screen]::PrimaryScreen.Bounds
$bitmap = New-Object System.Drawing.Bitmap $bounds.Width, $bounds.Height
$graphics = [System.Drawing.Graphics]::FromImage($bitmap)
$graphics.CopyFromScreen($bounds.Location, [System.Drawing.Point]::Empty, $bounds.Size)
$bitmap.Save("{target_file}", [System.Drawing.Imaging.ImageFormat]::Jpeg)
$graphics.Dispose()
$bitmap.Dispose()
"""
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps_script],
            check=True,
            capture_output=True,
            text=True
        )
        logger.info("已保存全屏截图：%s", target_file)
    except Exception as e:
        logger.error("保存全屏截图失败：%s", e)


def set_run_key(value: str, logger: logging.Logger) -> bool:
    """写入 HKCU\\...\\Run 启动项"""
    try:
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Run",
            0,
            winreg.KEY_ALL_ACCESS
        )
        winreg.SetValueEx(key, RUN_VALUE_NAME, 0, winreg.REG_SZ, value)
        winreg.CloseKey(key)
        logger.info("已写入启动项：%s = %s", RUN_VALUE_NAME, value)
        return True
    except Exception as e:
        logger.error("写入启动项失败：%s", e)
        return False


def delete_run_key(logger: logging.Logger) -> bool:
    """删除启动项"""
    try:
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Run",
            0,
            winreg.KEY_ALL_ACCESS
        )
        winreg.DeleteValue(key, RUN_VALUE_NAME)
        winreg.CloseKey(key)
        logger.info("已删除启动项：%s", RUN_VALUE_NAME)
        return True
    except FileNotFoundError:
        logger.info("启动项不存在，无需删除：%s", RUN_VALUE_NAME)
        return True
    except Exception as e:
        logger.error("删除启动项失败：%s", e)
        return False


def register_process(logger: logging.Logger, marker_file: str) -> None:
    """
    - 生成调试用 bat（含 chcp65001 + pause）
    - 启动项：脚本模式写 pythonw.exe + 脚本路径（无窗口）
    """
    logger.info("--- 正在检查安装路径与启动项 ---")

    script_path, is_exe = get_script_path()
    script_dir = os.path.dirname(script_path)

    # 1) 生成调试 bat
    write_debug_bat(script_path, sys.executable, script_dir, logger)

    # 2) 启动项目标：尽量无窗口
    if is_exe:
        target_run_value = f"\"{script_path}\""
    else:
        pythonw = guess_pythonw(sys.executable)
        target_run_value = f"\"{pythonw}\" \"{script_path}\""

    # 3) marker 比对（避免反复写注册表）
    stored = ""
    try:
        if os.path.exists(marker_file):
            with open(marker_file, "r", encoding="utf-8") as f:
                stored = f.read().strip()
    except Exception:
        stored = ""

    if stored.lower() != target_run_value.lower():
        try:
            with open(marker_file, "w", encoding="utf-8") as f:
                f.write(target_run_value)
            logger.info("已更新 marker 文件：%s", marker_file)
        except Exception as e:
            logger.error("写 marker 文件失败：%s", e)

        set_run_key(target_run_value, logger)
    else:
        logger.info("启动项无需更新。")


def format_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    return f"{h}小时{m}分{s}秒"


def get_total_write_bytes(proc_name: str) -> Tuple[int, int]:
    """
    注意：这里仍然使用 psutil 的 io_counters().write_bytes。
    也就是说，脚本里的“流量”本质上是“进程磁盘写入增量”，不是网卡流量。
    """
    total = 0
    count = 0
    for p in psutil.process_iter(["name"]):
        name = p.info.get("name")
        if name and name.lower() == proc_name.lower():
            try:
                io = p.io_counters()
                total += int(io.write_bytes)
                count += 1
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue
    return total, count


def sample_traffic(proc_name: str, last_total_write: Optional[int]) -> Tuple[int, int, float]:
    """
    返回:
      total_write: 当前累计 write_bytes
      proc_count:  匹配到的进程数
      traffic_delta_kb: 相比上一次采样，这一秒新增的 KB/s
    """
    total_write, proc_count = get_total_write_bytes(proc_name)

    if last_total_write is None or total_write < last_total_write:
        traffic_delta_kb = 0.0
    else:
        traffic_delta_kb = (total_write - last_total_write) / 1024.0

    return total_write, proc_count, traffic_delta_kb


def clear_console() -> None:
    if os.name == "nt":
        os.system("cls")
    else:
        sys.stdout.write("\033[2J\033[H")
        sys.stdout.flush()


def render_debugnet_screen(
    history: deque,
    history_seconds: int,
    current_kb: float,
    proc_count: int,
    in_session: bool,
    consecutive_zeros: int,
    mode_label: str = "运行中"
) -> None:
    """实时显示最近 N 秒流量历史"""
    clear_console()

    values = [kb for _, kb in history]
    avg_kb = sum(values) / len(values) if values else 0.0
    max_kb = max(values) if values else 0.0

    lines = [
        "=== UUYC Monitor / debugnet ===",
        "说明：这里显示的是当前脚本用于判定的“流量”——目标进程每秒 write_bytes 增量（KB/s），不是网卡流量。",
        f"模式: {mode_label}",
        f"当前时间: {datetime.datetime.now():%Y-%m-%d %H:%M:%S}",
        f"目标进程: {TARGET_PROCESS}",
        f"匹配进程数: {proc_count}",
        f"当前状态: {'连入中' if in_session else '空闲监控'}",
        f"当前瞬时流量: {current_kb:.2f} KB/s",
        f"连续近零秒数: {consecutive_zeros}",
        f"最近 {history_seconds} 秒平均: {avg_kb:.2f} KB/s",
        f"最近 {history_seconds} 秒峰值: {max_kb:.2f} KB/s",
        "-" * 72,
        f"最近 {history_seconds} 秒历史（最旧 -> 最新）:"
    ]

    if history:
        for ts, kb in history:
            lines.append(f"{ts:%H:%M:%S} | {kb:10.2f} KB/s")
    else:
        lines.append("(暂无样本)")

    sys.stdout.write("\n".join(lines) + "\n")
    sys.stdout.flush()


def run_startup_probe(
    logger: logging.Logger,
    startup_check_seconds: int,
    last_total_write: Optional[int],
    traffic_history: Optional[deque],
    debugnet_seconds: Optional[int]
) -> Tuple[datetime.datetime, datetime.datetime, Optional[int], int, bool, Optional[datetime.datetime]]:
    """
    启动时先监听前 startup_check_seconds 秒的流量：
    - 若结束时“不满足断开连接条件”（consecutive_zeros < ZERO_THRESHOLD），
      则认为程序启动时已经处于连接状态；
    - 且这 startup_check_seconds 秒要纳入最终连接时长统计。

    返回：
      startup_begin, startup_end, last_total_write, consecutive_zeros, starts_connected, detected_session_start
    """
    effective_seconds = max(startup_check_seconds, ZERO_THRESHOLD)
    if effective_seconds != startup_check_seconds:
        logger.warning(
            "startupcheck=%d 小于 ZERO_THRESHOLD=%d，已自动提升为 %d 秒",
            startup_check_seconds, ZERO_THRESHOLD, effective_seconds
        )

    startup_begin = datetime.datetime.now()
    logger.info("--- 启动判定：监听前 %d 秒流量以判断初始是否已处于连接状态 ---", effective_seconds)

    consecutive_zeros = 0
    detected_session_start: Optional[datetime.datetime] = None

    for i in range(effective_seconds):
        now = datetime.datetime.now()
        total_write, proc_count, traffic_delta_kb = sample_traffic(TARGET_PROCESS, last_total_write)
        last_total_write = total_write

        if traffic_delta_kb <= ZERO_KB_EPS:
            consecutive_zeros += 1
        else:
            consecutive_zeros = 0

        # 启动探测期间如果出现高流量，认为“刚刚开始连接”，直接结束启动探测
        if traffic_delta_kb > TRAFFIC_HIGH_THRESHOLD_KB:
            detected_session_start = now
            logger.info(
                "启动探测期间检测到高流量 %.2f KB/s，提前结束探测，判定为【刚刚开始连接】",
                traffic_delta_kb
            )
            if traffic_history is not None and debugnet_seconds is not None:
                traffic_history.append((now, traffic_delta_kb))
                render_debugnet_screen(
                    history=traffic_history,
                    history_seconds=debugnet_seconds,
                    current_kb=traffic_delta_kb,
                    proc_count=proc_count,
                    in_session=True,
                    consecutive_zeros=consecutive_zeros,
                    mode_label=f"启动判定中 ({i + 1}/{effective_seconds})"
                )
            break

        if traffic_history is not None and debugnet_seconds is not None:
            traffic_history.append((now, traffic_delta_kb))
            render_debugnet_screen(
                history=traffic_history,
                history_seconds=debugnet_seconds,
                current_kb=traffic_delta_kb,
                proc_count=proc_count,
                in_session=False,
                consecutive_zeros=consecutive_zeros,
                mode_label=f"启动判定中 ({i + 1}/{effective_seconds})"
            )

        if i < effective_seconds - 1:
            time.sleep(1)

    startup_end = datetime.datetime.now()

    if detected_session_start is not None:
        return startup_begin, startup_end, last_total_write, consecutive_zeros, True, detected_session_start

    # 如果到启动判定结束时，仍然“不满足断开连接条件”，
    # 就认为这段时间本来就在连接中
    starts_connected = consecutive_zeros < ZERO_THRESHOLD

    if starts_connected:
        logger.info(
            "启动判定结果：最近 %d 秒不满足断开连接条件，判定为【启动时已处于连接状态】；连接开始时间回溯到 %s",
            effective_seconds,
            startup_begin.strftime("%Y-%m-%d %H:%M:%S")
        )
    else:
        logger.info(
            "启动判定结果：最近 %d 秒满足断开连接条件，判定为【启动时为空闲状态】",
            effective_seconds
        )

    return startup_begin, startup_end, last_total_write, consecutive_zeros, starts_connected, None


def monitor_system(
    logger: logging.Logger,
    marker_file: str,
    debugnet_seconds: Optional[int] = None,
    startup_check_seconds: int = DEFAULT_STARTUP_CHECK_SECONDS,
    debug_switches: Optional[set] = None
) -> None:
    # 如果你不想运行时碰启动项，可以把这行注释掉
    register_process(logger, marker_file)

    logger.info("--- 开始监控进程: %s ---", TARGET_PROCESS)

    if debugnet_seconds is not None:
        logger.info(
            "--- debugnet 已启用：屏幕显示最近 %d 秒实时流量历史 ---",
            debugnet_seconds
        )

    # 初始状态变量
    in_session = False
    session_start_time: Optional[datetime.datetime] = None
    timeout_timer_start: Optional[datetime.datetime] = None
    last_total_write: Optional[int] = None
    timeout_hits = 0
    shutdown_scheduled = False
    debug_switches = debug_switches or set()
    offline_logged = False
    next_screenshot_time = datetime.datetime.now()

    traffic_history = deque(maxlen=debugnet_seconds) if debugnet_seconds is not None else None

    # 0) 启动判定：先监听前 X 秒（若已离线则跳过）
    if is_offline_mode():
        startup_begin = datetime.datetime.now()
        startup_end = startup_begin
        consecutive_zeros = 0
        starts_connected = False
        detected_session_start = None
        logger.warning("启动时检测到 %s，跳过启动探测，进入离线保活模式。", DISABLE_FILE_PATH)
    else:
        startup_begin, startup_end, last_total_write, consecutive_zeros, starts_connected, detected_session_start = run_startup_probe(
            logger=logger,
            startup_check_seconds=startup_check_seconds,
            last_total_write=last_total_write,
            traffic_history=traffic_history,
            debugnet_seconds=debugnet_seconds
        )

    if starts_connected:
        # 认为启动时就已经在连接中，并把最开始这段时间纳入总连接时长
        in_session = True
        session_start_time = detected_session_start or startup_begin
        timeout_timer_start = None
        if detected_session_start is not None:
            logger.info(
                "[系统启动] 启动探测期间出现高流量，连接开始时间按探测命中时刻计：%s",
                detected_session_start.strftime("%Y-%m-%d %H:%M:%S")
            )
        else:
            logger.info(
                "[系统启动] 初始状态判定为连接中，连接开始时间按程序启动时刻计：%s",
                startup_begin.strftime("%Y-%m-%d %H:%M:%S")
            )
    else:
        # 认为启动时为空闲；启动监听期也计入空闲超时
        in_session = False
        session_start_time = None
        timeout_timer_start = startup_begin
        logger.info(
            "[系统启动] 初始状态判定为空闲，空闲超时计时从程序启动时刻开始：%s",
            startup_begin.strftime("%Y-%m-%d %H:%M:%S")
        )

    # debug 项：立即测试关机提醒链路
    if "shutdown" in debug_switches and not shutdown_scheduled:
        if is_offline_mode():
            logger.warning("[DEBUG] --debug-shutdown 已请求，但当前为离线模式，按规则跳过。")
        else:
            shutdown_scheduled = True
            logger.warning("[DEBUG] --debug-shutdown 已启用：立即触发关机提醒与关机命令测试。")
            if is_reminder_enabled("shutdown_warning") and (not is_offline_mode()):
                show_shutdown_warning_popup(logger)
                send_pushdeer(
                    "关机提醒（DEBUG）",
                    f"已触发 --debug-shutdown，执行 shutdown -s -t {SHUTDOWN_COMMAND_SECONDS}。",
                    logger=logger
                )
            else:
                logger.info("提醒关闭或离线模式：跳过 DEBUG 关机提醒弹窗与推送。")
            schedule_forced_shutdown(logger)

    # 1) 正常监控循环
    while True:
        now = datetime.datetime.now()

        # 周期截图（无论是否离线都执行）
        if now >= next_screenshot_time:
            capture_fullscreen_to_jpg(SCREENSHOT_SAVE_DIR, logger)
            ensure_screenshot_retention(SCREENSHOT_SAVE_DIR, SCREENSHOT_RETENTION_DAYS, logger)
            next_screenshot_time = now + datetime.timedelta(minutes=SCREENSHOT_INTERVAL_MINUTES)

        # 离线模式：仅保活 + 截图，不执行其它监控/提醒逻辑
        if is_offline_mode():
            if not offline_logged:
                logger.warning("检测到 %s，进入离线模式：暂停监控与提醒，仅保活和周期截图。", DISABLE_FILE_PATH)
                offline_logged = True
            time.sleep(1)
            continue
        if offline_logged:
            logger.info("离线模式已解除（%s 不存在），恢复正常监控。", DISABLE_FILE_PATH)
            offline_logged = False

        total_write, proc_count, traffic_delta_kb = sample_traffic(TARGET_PROCESS, last_total_write)
        last_total_write = total_write

        # “近似0流量”计数（用于连入结束判定）
        if traffic_delta_kb <= ZERO_KB_EPS:
            consecutive_zeros += 1
        else:
            consecutive_zeros = 0

        # 1) 检测高流量 -> 进入连入状态
        if traffic_delta_kb > TRAFFIC_HIGH_THRESHOLD_KB:
            if not in_session:
                in_session = True
                session_start_time = now
                timeout_timer_start = None
                timeout_hits = 0
                logger.info(
                    ">>> [连入开始] 高流量 %.2f KB/s，匹配进程数=%d",
                    traffic_delta_kb, proc_count
                )
                # 如需“连入开始”也通知，可取消注释下一行
                # send_pushdeer("连入开始", f"检测到高流量：{traffic_delta_kb:.2f} KB/s", logger=logger)

        # 2) 检测连入结束 -> 恢复超时倒计时
        if in_session and consecutive_zeros >= ZERO_THRESHOLD:
            end_time = now
            duration = (end_time - session_start_time).total_seconds() if session_start_time else 0

            msg = f"时长: {format_duration(duration)}"
            if is_reminder_enabled("session_end") and (not is_offline_mode()):
                send_pushdeer("连入流程结束", msg, logger=logger)
            else:
                logger.info("提醒关闭或离线模式：跳过“连入流程结束”推送。")
            logger.info("<<< [连入结束] %s", msg)

            in_session = False
            session_start_time = None
            consecutive_zeros = 0

            timeout_timer_start = datetime.datetime.now()
            logger.info(">>> [状态切换] 重新开始 %d分钟 超时倒计时", TIMEOUT_MINUTES)

        # 3) 超时监控（只要不在连入状态，就一直检查）
        if not in_session:
            if timeout_timer_start is None:
                timeout_timer_start = now

            elapsed = (now - timeout_timer_start).total_seconds()
            if elapsed >= TIMEOUT_MINUTES * 60:
                if is_reminder_enabled("timeout_warning") and (not is_offline_mode()):
                    send_pushdeer(
                        "设备超时警告",
                        f"设备空闲已超过 {TIMEOUT_MINUTES} 分钟。",
                        logger=logger
                    )
                else:
                    logger.info("提醒关闭或离线模式：跳过“设备超时警告”推送。")
                logger.warning(
                    "!!! [超时触发] 已空闲 %d 分钟，发送通知并重置",
                    TIMEOUT_MINUTES
                )
                timeout_hits += 1
                logger.warning(
                    "!!! [超时累计] 连续空闲超时次数：%d/%d",
                    timeout_hits, SHUTDOWN_TRIGGER_TIMEOUT_HITS
                )

                if (not shutdown_scheduled) and timeout_hits >= SHUTDOWN_TRIGGER_TIMEOUT_HITS:
                    shutdown_scheduled = True
                    if is_reminder_enabled("shutdown_warning") and (not is_offline_mode()):
                        show_shutdown_warning_popup(logger)
                        send_pushdeer(
                            "关机提醒",
                            f"设备已连续空闲超过 2 小时，已执行 shutdown -s -t {SHUTDOWN_COMMAND_SECONDS}。",
                            logger=logger
                        )
                    else:
                        logger.info("提醒关闭或离线模式：跳过关机提醒弹窗与推送。")
                    schedule_forced_shutdown(logger)

                timeout_timer_start = datetime.datetime.now()

        # 4) debugnet 屏幕显示
        if traffic_history is not None and debugnet_seconds is not None:
            traffic_history.append((now, traffic_delta_kb))
            render_debugnet_screen(
                history=traffic_history,
                history_seconds=debugnet_seconds,
                current_kb=traffic_delta_kb,
                proc_count=proc_count,
                in_session=in_session,
                consecutive_zeros=consecutive_zeros,
                mode_label="正常运行中"
            )

        time.sleep(1)


def main():
    parser = argparse.ArgumentParser(description="UUYC Monitor")

    parser.add_argument(
        "-debug", "--debug",
        action="store_true",
        help="调试模式：输出调试日志。若同时启用 -debugnet，则调试日志改写到运行目录/uuyc-monitor.log"
    )
    parser.add_argument(
        "-debugnet", "--debugnet",
        type=positive_int,
        metavar="秒数",
        help="在屏幕实时显示最近 N 秒的流量历史（按现有逻辑为进程 write_bytes 增量，单位 KB/s）"
    )
    parser.add_argument(
        "-startupcheck", "--startupcheck",
        type=positive_int,
        default=DEFAULT_STARTUP_CHECK_SECONDS,
        metavar="秒数",
        help="启动时先监听 N 秒流量，用于判断程序启动时是否已处于连接状态；默认 60 秒"
    )
    parser.add_argument(
        "-install", "--install",
        action="store_true",
        help="只安装/修复启动项，然后退出"
    )
    parser.add_argument(
        "-uninstall", "--uninstall",
        action="store_true",
        help="卸载启动项，然后退出"
    )

    args, unknown_args = parser.parse_known_args()
    debug_switches = parse_debug_switches(unknown_args)
    unknown_non_debug = [x for x in unknown_args if not x.startswith("--debug-")]
    if unknown_non_debug:
        parser.error(f"无法识别的参数：{' '.join(unknown_non_debug)}")

    logger = setup_logging(args.debug, args.debugnet)
    marker_file = resolve_marker_file(logger)
    if debug_switches:
        logger.warning("已启用自定义 debug 开关：%s", ", ".join(sorted(debug_switches)))

    if args.debug and args.debugnet is not None:
        logger.info(
            "debug + debugnet 同时启用：普通调试日志已改写到 %s",
            get_debug_console_redirect_log()
        )

    if args.uninstall:
        delete_run_key(logger)
        try:
            if os.path.exists(marker_file):
                os.remove(marker_file)
                logger.info("已删除 marker 文件：%s", marker_file)
        except Exception as e:
            logger.warning("marker 文件删除失败：%s", e)
        return

    if args.install:
        register_process(logger, marker_file)
        return

    try:
        monitor_system(
            logger,
            marker_file,
            debugnet_seconds=args.debugnet,
            startup_check_seconds=args.startupcheck,
            debug_switches=debug_switches
        )
    except KeyboardInterrupt:
        logger.info("收到 Ctrl+C，程序退出。")


if __name__ == "__main__":
    main()
