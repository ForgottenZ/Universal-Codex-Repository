# Teaching Week Calendar & Notifier

一个 Python WebUI 程序，支持教学周日历、单次/循环/覆盖事件、Pushdeer 与 Microsoft Graph 邮件提醒、可配置周报模板与变量替换。

## 功能

- 教学周日历视图（按教学周展示周一到周日）。
- 事件类型：
  - **single**：第 X 教学周 / 周 X / 时分触发；
  - **recurring**：从起点开始，按 `X周 + X日 + X小时 + X分钟` 循环触发；
  - **coverage**：从指定起始到结束时间视为“发生中”。
- 提醒通道：
  - Pushdeer（通过 pushkey）；
  - Microsoft Graph（OAuth2 设备码登录 Outlook 后发信）。
- 每周报告：
  - 默认每周一 12:00 发送，可配置多个时间点；
  - 可展示当前教学周、本周事件、未来 N 周事件、备注说明；
  - 支持模板变量：
    - `{UserNickname}`
    - `{NowTeachWeek}`
    - `{MaxTeachWeek}`
    - `{WeekProgressBar}`
    - `{FutureWeeks}`
    - `{CurrentWeekEvents}`
    - `{FutureEvents}`
    - `{EventNotes}`
- 自动配置：
  - 若 `config.yaml` 不存在会自动生成默认配置；
  - 若未启用 MySQL 或无 MySQL 配置，自动使用 SQLite。

## 运行

```bash
python3 -m pip install -r requirements.txt
python3 app.py
```

默认地址：`http://127.0.0.1:8000`

## 配置说明

程序首次启动会生成 `config.yaml`。核心项：

- `user.username`：真名（Username）
- `user.user_nickname`：称呼（UserNickname）
- `term.start_date/end_date/max_teach_week`：教学周范围
- `database.enable_mysql/mysql_url`：MySQL 开关与连接串
- `notify.pushdeer`：Pushdeer 开关与 key
- `notify.microsoft_graph`：Graph OAuth2 参数与收件人
- `report.schedules`：周报发送计划，例如 `["Mon 12:00", "Tue 18:30"]`
- `report.template`：周报模板

## Graph OAuth2 说明

首次邮件发送时，如果本地 token 缓存不可用，会在服务端控制台打印设备码登录提示：

- 打开 `verification_uri`
- 输入 `user_code`

完成后 token 将写入 `token_cache` 配置对应文件，后续自动静默续期。
