import json
import threading
from pathlib import Path
from tkinter import Tk, StringVar, END
from tkinter import ttk, messagebox

import requests

API_URL = "https://api2.pushdeer.com/message/push"
CONFIG_PATH = Path.home() / ".pushdeer_gui_config.json"


class PushDeerApp:
    def __init__(self, root: Tk) -> None:
        self.root = root
        self.root.title("PushDeer 通知发送器")
        self.root.geometry("640x480")

        self.pushkey_var = StringVar()
        self.text_var = StringVar()
        self.type_var = StringVar(value="markdown")
        self.timeout_var = StringVar(value="10")

        self._build_ui()
        self._load_config()

    def _build_ui(self) -> None:
        frame = ttk.Frame(self.root, padding=16)
        frame.pack(fill="both", expand=True)

        ttk.Label(frame, text="PushDeer Key:").grid(row=0, column=0, sticky="w")
        ttk.Entry(frame, textvariable=self.pushkey_var, width=60, show="*").grid(
            row=1, column=0, columnspan=2, sticky="ew", pady=(4, 10)
        )

        ttk.Label(frame, text="通知标题:").grid(row=2, column=0, sticky="w")
        ttk.Entry(frame, textvariable=self.text_var, width=60).grid(
            row=3, column=0, columnspan=2, sticky="ew", pady=(4, 10)
        )

        ttk.Label(frame, text="消息内容 (desp):").grid(row=4, column=0, sticky="w")
        self.desp_text = ttk.Treeview  # type: ignore[assignment]
        from tkinter import Text

        self.desp_text = Text(frame, height=12, wrap="word")
        self.desp_text.grid(row=5, column=0, columnspan=2, sticky="nsew", pady=(4, 10))

        option_bar = ttk.Frame(frame)
        option_bar.grid(row=6, column=0, columnspan=2, sticky="ew")

        ttk.Label(option_bar, text="消息类型:").pack(side="left")
        ttk.Combobox(
            option_bar,
            textvariable=self.type_var,
            values=["markdown", "text"],
            width=12,
            state="readonly",
        ).pack(side="left", padx=(6, 16))

        ttk.Label(option_bar, text="超时(秒):").pack(side="left")
        ttk.Entry(option_bar, textvariable=self.timeout_var, width=8).pack(side="left", padx=(6, 0))

        button_bar = ttk.Frame(frame)
        button_bar.grid(row=7, column=0, columnspan=2, sticky="e", pady=(16, 0))

        self.send_button = ttk.Button(button_bar, text="发送通知", command=self.send_message)
        self.send_button.pack(side="right", padx=(8, 0))

        ttk.Button(button_bar, text="保存配置", command=self._save_config).pack(side="right")
        ttk.Button(button_bar, text="清空内容", command=self.clear_form).pack(side="right", padx=(0, 8))

        self.status_var = StringVar(value="就绪")
        ttk.Label(frame, textvariable=self.status_var).grid(
            row=8, column=0, columnspan=2, sticky="w", pady=(12, 0)
        )

        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(5, weight=1)

    def clear_form(self) -> None:
        self.text_var.set("")
        self.desp_text.delete("1.0", END)
        self.status_var.set("已清空输入")

    def _load_config(self) -> None:
        if not CONFIG_PATH.exists():
            return
        try:
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            self.pushkey_var.set(data.get("pushkey", ""))
            self.type_var.set(data.get("type", "markdown"))
            self.timeout_var.set(str(data.get("timeout", 10)))
            self.status_var.set("已加载本地配置")
        except Exception as exc:  # noqa: BLE001
            self.status_var.set(f"读取配置失败: {exc}")

    def _save_config(self) -> None:
        config = {
            "pushkey": self.pushkey_var.get().strip(),
            "type": self.type_var.get().strip() or "markdown",
            "timeout": self._parse_timeout(fallback=10),
        }
        CONFIG_PATH.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
        self.status_var.set(f"配置已保存到 {CONFIG_PATH}")

    def _parse_timeout(self, fallback: int) -> int:
        try:
            timeout = int(self.timeout_var.get().strip())
            if timeout <= 0:
                raise ValueError
            return timeout
        except ValueError:
            return fallback

    def send_message(self) -> None:
        pushkey = self.pushkey_var.get().strip()
        text = self.text_var.get().strip()
        desp = self.desp_text.get("1.0", END).strip()
        msg_type = self.type_var.get().strip() or "markdown"
        timeout = self._parse_timeout(fallback=10)

        if not pushkey:
            messagebox.showwarning("参数不完整", "请先输入 PushDeer Key")
            return
        if not text:
            messagebox.showwarning("参数不完整", "请先输入通知标题")
            return

        payload = {
            "pushkey": pushkey,
            "text": text,
            "desp": desp,
            "type": msg_type,
        }

        self.send_button.configure(state="disabled")
        self.status_var.set("发送中...")

        def task() -> None:
            try:
                response = requests.post(API_URL, data=payload, timeout=timeout)
                response.raise_for_status()
                result = response.json()
                if result.get("errorCode") == 0:
                    self._on_send_done(True, "发送成功")
                else:
                    self._on_send_done(False, f"发送失败: {result}")
            except Exception as exc:  # noqa: BLE001
                self._on_send_done(False, f"发送异常: {exc}")

        threading.Thread(target=task, daemon=True).start()

    def _on_send_done(self, success: bool, message: str) -> None:
        def update_ui() -> None:
            self.send_button.configure(state="normal")
            self.status_var.set(message)
            if success:
                messagebox.showinfo("PushDeer", message)
            else:
                messagebox.showerror("PushDeer", message)

        self.root.after(0, update_ui)


if __name__ == "__main__":
    root = Tk()
    app = PushDeerApp(root)
    root.mainloop()
