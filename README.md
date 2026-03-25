# Universal-Codex-Repository

## PushDeer 通知发送程序（带 UI）

这是一个基于 Python + Tkinter 的桌面程序，用于发送 PushDeer 通知。

### 功能特性
- 图形界面输入 PushDeer Key、通知标题、正文内容。
- 支持 `markdown` / `text` 两种消息类型。
- 支持自定义请求超时秒数。
- 发送过程放在后台线程，不会卡住界面。
- 支持本地保存与加载常用配置（保存到用户目录）。

### 运行环境
- Python 3.9+
- 依赖见 `requirements.txt`

### 安装依赖
```bash
pip install -r requirements.txt
```

### 启动程序
```bash
python pushdeer_gui.py
```

### 使用说明
1. 填写 `PushDeer Key`。
2. 填写通知标题。
3. 在消息内容框中输入正文（可为空）。
4. 选择消息类型（`markdown` 或 `text`）。
5. 点击“发送通知”。

### 配置文件
- 点击“保存配置”后，会写入：
  - Linux/macOS: `~/.pushdeer_gui_config.json`
  - Windows: `%USERPROFILE%\\.pushdeer_gui_config.json`

### 注意事项
- 请妥善保管你的 PushDeer Key。
- 如发送失败，可查看界面底部状态信息及弹窗错误提示。
