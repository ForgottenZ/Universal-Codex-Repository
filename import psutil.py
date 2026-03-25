import argparse
import datetime
import logging
import os
import sys
import time
from collections import deque
from logging.handlers import RotatingFileHandler
from typing import Optional, Tuple

import psutil
import requests
import winreg

# ================= 配置区 =================
PUSH_KEY = "你的PushDeer_Key_填在这里"
TARGET_PROCESS = "gameviewerserver.exe"

# 你原来的 marker 文件路径（如果这里不可写，会自动回退到 APPDATA）
DEFAULT_MARKER_FILE = r"D:\uuyc-notify.luobo"

TRAFFIC_HIGH_THRESHOLD_KB = 100   # KB/s，超过认为“连入开始”
ZERO_THRESHOLD = 30               # 连续“近似0流量”的秒数，认为“连入结束”
ZERO_KB_EPS = 1.0                 # <= 1KB/s 都当作 0（防止极小写入打断计数）
TIMEOUT_MINUTES = 20              # 空闲超时提醒（分钟）

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

    try:
        with open(bat_path, "w", encoding="utf-8") as f:
            f.write(content)
        logger.info("已生成调试 BAT：%s", bat_path)
    except Exception as e:
        logger.error("生成 BAT 失败：%s", e)


def send_pushdeer(text: str, desp: str = "", logger: Optional[logging.Logger] = None) -> None:
    if not PUSH_KEY or "填在这里" in PUSH_KEY:
        if logger:
            logger.warning("PushDeer Key 未设置，跳过发送：%s", text)
        return

    url = "https://api2.pushdeer.com/message/push"
    try:
        r = requests.get(
            url,
            params={"pushkey": PUSH_KEY, "text": text, "desp": desp},
            timeout=5
        )
        if logger:
            logger.info("[通知] 已发送：%s（HTTP %s）", text, r.status_code)
    except Exception as e:
        if logger:
            logger.error("[错误] 通知发送失败：%s", e)


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
    consecutive_zeros: int
) -> None:
    """实时显示最近 N 秒流量历史"""
    clear_console()

    values = [kb for _, kb in history]
    avg_kb = sum(values) / len(values) if values else 0.0
    max_kb = max(values) if values else 0.0

    lines = [
        "=== UUYC Monitor / debugnet ===",
        "说明：这里显示的是当前脚本用于判定的“流量”——目标进程每秒 write_bytes 增量（KB/s），不是网卡流量。",
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


def monitor_system(
    logger: logging.Logger,
    marker_file: str,
    debugnet_seconds: Optional[int] = None
) -> None:
    # 如果你不想运行时碰启动项，可以把这行注释掉
    register_process(logger, marker_file)

    logger.info("--- 开始监控进程: %s ---", TARGET_PROCESS)

    if debugnet_seconds is not None:
        logger.info(
            "--- debugnet 已启用：屏幕显示最近 %d 秒实时流量历史 ---",
            debugnet_seconds
        )

    in_session = False
    session_start_time: Optional[datetime.datetime] = None
    consecutive_zeros = 0

    # 默认启动就进入“空闲超时监控”
    timeout_timer_start = datetime.datetime.now()
    logger.info("[系统启动] 默认进入空闲超时监控...")

    last_total_write: Optional[int] = None
    traffic_history = deque(maxlen=debugnet_seconds) if debugnet_seconds is not None else None

    while True:
        now = datetime.datetime.now()
        total_write, proc_count = get_total_write_bytes(TARGET_PROCESS)

        # 计算 delta（KB/s）
        if last_total_write is None or total_write < last_total_write:
            traffic_delta_kb = 0.0
        else:
            traffic_delta_kb = (total_write - last_total_write) / 1024.0
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
                logger.info(
                    ">>> [连入开始] 高流量 %.2f KB/s，匹配进程数=%d",
                    traffic_delta_kb, proc_count
                )
                # send_pushdeer("连入开始", f"检测到高流量：{traffic_delta_kb:.2f} KB/s", logger=logger)

        # 2) 检测连入结束 -> 恢复超时倒计时
        if in_session and consecutive_zeros >= ZERO_THRESHOLD:
            end_time = now
            duration = (end_time - session_start_time).total_seconds() if session_start_time else 0

            msg = f"时长: {format_duration(duration)}"
            send_pushdeer("连入流程结束", msg, logger=logger)
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
                send_pushdeer(
                    "设备超时警告",
                    f"设备空闲已超过 {TIMEOUT_MINUTES} 分钟。",
                    logger=logger
                )
                logger.warning(
                    "!!! [超时触发] 已空闲 %d 分钟，发送通知并重置",
                    TIMEOUT_MINUTES
                )
                timeout_timer_start = datetime.datetime.now()

        # 4) debugnet 屏幕显示
        if traffic_history is not None:
            traffic_history.append((now, traffic_delta_kb))
            render_debugnet_screen(
                history=traffic_history,
                history_seconds=debugnet_seconds,
                current_kb=traffic_delta_kb,
                proc_count=proc_count,
                in_session=in_session,
                consecutive_zeros=consecutive_zeros
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
        "-install", "--install",
        action="store_true",
        help="只安装/修复启动项，然后退出"
    )
    parser.add_argument(
        "-uninstall", "--uninstall",
        action="store_true",
        help="卸载启动项，然后退出"
    )

    args = parser.parse_args()

    logger = setup_logging(args.debug, args.debugnet)
    marker_file = resolve_marker_file(logger)

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

    monitor_system(logger, marker_file, debugnet_seconds=args.debugnet)


if __name__ == "__main__":
    main()