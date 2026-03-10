# 教学周日历与提醒系统（WebUI）

这是一个基于 Python 的教学周日历管理程序，包含 WebUI、事件调度、每周报告、Pushdeer 通知、Microsoft Graph 邮件通知，并支持 MySQL/SQLite 自动切换。

## 功能概览

- 教学周日历展示（按“第X教学周 + 周X”显示）。
- 可配置学期开始/结束日期，自动计算最大教学周。
- 事件系统：
  - **单次事件**：第X教学周-周X-时分触发。
  - **循环事件**：按 X教学周 / X天 / X小时 / X分钟 间隔循环触发。
  - **覆盖事件**：指定开始与结束时刻，在区间内视为“发生中”。
- 提醒通道：
  - Pushdeer（需 pushkey）
  - Microsoft Graph（OAuth2 应用凭据）
- 每周报告：
  - 默认每周一 12:00 发送（可配置多个发送时间）
  - 报告可使用变量模板并在发送时替换
  - 展示“当前教学周”“本周事件”“未来X周事件”“备注摘要”
- 配置文件自动生成：首次运行若无 `config.yaml`，自动生成默认配置。
- 数据库自动选择：
  - 若 `config.yaml` 的 `database.mysql.enabled=true` 使用 MySQL
  - 否则自动使用 SQLite（`calendar.db`）

---

## 项目结构

```bash
.
├── app.py                # 主程序（Web + 调度 + 通知 + 数据模型）
├── templates/
│   └── index.html        # WebUI 页面
├── notify01.py           # 你提供的参考通知脚本（保留）
├── requirements.txt      # 依赖
└── README.md
```

---

## 环境要求

- Python 3.10+
- Linux/macOS/Windows 均可
- 可选 MySQL（若不用，默认 SQLite 无需安装数据库）

---

## Build / 运行步骤（详细）

### 1) 克隆并进入目录

```bash
git clone <your-repo-url>
cd Universal-Codex-Repository
```

### 2) 创建虚拟环境（推荐）

```bash
python -m venv .venv
source .venv/bin/activate
# Windows PowerShell: .venv\Scripts\Activate.ps1
```

### 3) 安装依赖

```bash
pip install -r requirements.txt
```

### 4) 首次启动（自动生成配置）

```bash
python app.py
```

首次启动后会自动生成 `config.yaml`（若不存在），并创建 SQLite 数据库 `calendar.db`。

### 5) 打开 WebUI

浏览器访问：

```text
http://127.0.0.1:5000
```

---

## 配置说明（config.yaml）

重点字段：

- `user.username`：真名（用户名）
- `user.nickname`：称呼（UserNickname）
- `teaching_calendar.start_date` / `end_date`
- `notify.pushdeer.enabled` + `notify.pushdeer.pushkey`
- `notify.microsoft_graph.*`：Graph 发信配置
- `weekly_report.schedules`：每周报告发送时间规则列表
- `weekly_report.template`：报告模板

### 报告模板变量

可在 `weekly_report.template` 使用如下变量：

- `{UserNickname}`
- `{NowTeachWeek}`
- `{MaxTeachWeek}`
- `{ProgressBar}`（10格进度条）
- `{CurrentWeekEvents}`
- `{FutureWeeks}`
- `{FutureEvents}`
- `{Notes}`

---

## Microsoft Graph 说明

当前程序使用 **应用凭据（client credentials）** 发送邮件：

1. 在 Azure Entra 注册应用。
2. 配置 `client_id` / `client_secret` / `tenant_id`。
3. 为 Graph API 添加 `Mail.Send` 应用权限并管理员同意。
4. 配置 `sender_email`（发送账户）。

如你后续需要“用户登录式 OAuth2（交互登录）”，可在现有基础上扩展为授权码流或设备码流。

---

## 你提出的需求对照

- [x] WebUI + 教学周日历
- [x] 开始/结束时间配置
- [x] 单次事件（第X教学周、周X、时分）
- [x] 循环事件（教学周/天/小时/分钟）
- [x] 覆盖事件（开始到结束）
- [x] 每周报告（可配置，多规则）
- [x] 报告模板变量替换
- [x] Pushdeer + Microsoft Graph 通知
- [x] MySQL/SQLite 自动切换
- [x] 无配置文件自动生成
- [x] 区分 `username` 与 `nickname`

---

## 我额外增加的实用点

1. **防重复发送机制（NotifyLog）**：同一触发点不会重复提醒。  
2. **覆盖事件进度条展示**：在周报中显示覆盖事件区间进度。  
3. **统一在 WebUI 管理模板和基础配置**：降低维护成本。

---

## 测试建议

可先创建一个“当前分钟触发”的单次事件，勾选提醒并观察日志/通知结果。

也可通过修改系统时间或教学周起始日期，快速验证周报与教学周计算逻辑。

---

## 启动命令

```bash
python app.py
```

默认监听 `0.0.0.0:5000`。
