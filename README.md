# Universal-Codex-Repository - 教学周日历提醒系统

本分支实现了一个 Python WebUI 程序，支持：

1. **教学周日历与事件系统**
   - 配置学期开始时间/结束时间。
   - 以“第 X 教学周 / 周 X / X 时 X 分”定义触发时间。
   - 支持普通事件、循环事件（每 X 教学周 + X 日 + X 小时 + X 分钟）、覆盖事件（开始到结束区间内持续发生中）。
   - 支持事件触发时选择是否提醒，以及提醒内容。

2. **每周报告系统**
   - 默认每周一中午 12:00 发送，可在配置中设置多个时间点。
   - 报告会告知当前是第几个教学周、本周事件、未来 N 周可能发生的事件。
   - 报告模板支持变量：
     - `{{current_week}}`
     - `{{this_week_events}}`
     - `{{lookahead_weeks}}`
     - `{{upcoming_events}}`
     - `{{now}}`

3. **提醒通道**
   - **PushDeer**：使用 `pushkey`。
   - **Microsoft Graph**：通过 OAuth2（MSAL 设备码登录）获取 token，调用 Outlook `sendMail` 发送邮件。
   - 发送逻辑参考并复用 `notify01.py` 的 Graph + PushDeer 思路。

4. **数据库与配置**
   - 使用 `config.yaml` 配置。
   - 若未检测到配置文件，会自动生成默认配置。
   - 若 `database.mysql.enabled=false` 或未正确配置 MySQL，则使用 SQLite。

---

## 项目结构

```text
.
├── app.py
├── notify01.py
├── requirements.txt
└── README.md
```

---

## Build / Run

### 1) 创建虚拟环境并安装依赖

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2) 启动程序

```bash
python app.py
```

默认监听：`http://0.0.0.0:8000`

首次启动会自动生成 `config.yaml`（若不存在）。

---

## 配置文件说明（config.yaml）

程序启动时会生成类似如下的配置：

- `app.host` / `app.port`: Web 服务地址。
- `database.mysql.enabled`: 是否启用 MySQL。
- `database.sqlite.path`: SQLite 文件路径。
- `term.start` / `term.end`: 教学周映射基准。
- `report.times`: 周报发送时间列表，格式如 `Mon 12:00`，可多个。
- `report.lookahead_weeks`: 未来预测周数。
- `report.template`: 周报模板（变量替换）。
- `notify.pushdeer`: PushDeer 配置。
- `notify.graph`: Graph OAuth2 + 邮件参数。

---

## 使用说明

### 1) 教学周日历

打开首页可查看“第 N 教学周”表格（周一到周日），风格与示例图一致（周末高亮）。

### 2) 添加事件

在“新增事件”区域填写：
- 标题 / 描述
- 开始：第几教学周、周几、时分
- 可选结束时间（填了即为覆盖事件）
- 是否提醒 + 提醒文本
- 循环周期（周/日/时/分）

### 3) 周报

系统后台每分钟检查调度，命中配置时间点时发送周报。

### 4) 配置接口

- `GET /api/config` 查看当前配置
- `POST /api/config` 更新配置（JSON）
- `GET /api/report/preview` 预览周报文本

---

## 开发与测试建议

1. 先使用 SQLite（默认）验证功能。
2. 如需 MySQL：
   - 安装并启动 MySQL
   - `pip install PyMySQL`
   - 在 `config.yaml` 中开启 `database.mysql.enabled=true`
3. PushDeer/Graph 均可先关闭，通过页面事件功能与报告预览完成业务验证。

---

## 说明

- 当前实现采用 APScheduler 后台轮询（每分钟）实现事件触发与周报发送。
- Graph 登录采用 **设备码登录流程**，首次发送时会在控制台输出登录提示。

如果你后续希望，我可以继续扩展：
- 事件编辑页面
- 前端拖拽日历
- 更丰富的报告变量（如“按课程分类统计”等）
- 企业微信/钉钉/飞书通知通道
