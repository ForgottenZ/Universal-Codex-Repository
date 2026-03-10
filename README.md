# 教学周日历与提醒系统（WebUI）

这是一个基于 Python 的教学周日历管理程序，包含 WebUI、登录鉴权、事件调度、每周报告、Pushdeer 通知、Microsoft Graph 邮件通知，并支持 MySQL/SQLite 自动切换。

## 功能概览

- 登录系统：未登录不可访问管理页面与操作接口。
- 教学周日历展示（按“第X教学周 + 周X”显示）。
- 可配置学期开始/结束日期，自动计算最大教学周。
- 事件系统：
  - **单次事件**：第X教学周-周X-时分触发。
  - **循环事件**：按 X教学周 / X天 / X小时 / X分钟 间隔循环触发。
  - **覆盖事件**：指定开始与结束时刻，在区间内视为“发生中”。
- 新增事件时按类型动态显示字段：
  - 单次：只显示基础时间字段
  - 循环：显示循环间隔字段
  - 覆盖：显示结束时间字段
- 提醒通道：
  - Pushdeer（需 pushkey）
  - Microsoft Graph（OAuth2 应用凭据）
- 每周报告：
  - 默认每周一 12:00 发送（可配置多个发送时间）
  - 报告可使用变量模板并在发送时替换
- 测试通知：可在页面手动发送测试消息（选择 Pushdeer / Email）。
- 配置文件自动生成：首次运行若无 `config.yaml`，自动生成默认配置。
- 数据库自动选择：
  - 若 `config.yaml` 的 `database.mysql.enabled=true` 使用 MySQL
  - 否则自动使用 SQLite（`calendar.db`）
- 修复 APScheduler 上下文问题：后台任务使用 `app.app_context()` 避免 “Working outside of application context” 报错。

## 项目结构

```bash
.
├── app.py
├── templates/
│   ├── index.html
│   └── login.html
├── notify01.py
├── requirements.txt
└── README.md
```

## Build / 运行步骤

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python app.py
```

打开：`http://127.0.0.1:5000`

默认登录账号密码（首次自动生成配置后）：
- username: `admin`
- password: `admin123`

> 请上线前在 `config.yaml` 中修改 `auth.username` / `auth.password`。

## 配置重点

- `auth.username` / `auth.password`：系统登录账号
- `user.username`：真名
- `user.nickname`：称呼
- `notify.pushdeer.*`
- `notify.microsoft_graph.*`
- `weekly_report.schedules`
- `weekly_report.template`

## 模板变量

`{UserNickname}` `{NowTeachWeek}` `{MaxTeachWeek}` `{ProgressBar}` `{CurrentWeekEvents}` `{FutureWeeks}` `{FutureEvents}` `{Notes}`

## 启动命令

```bash
python app.py
```
