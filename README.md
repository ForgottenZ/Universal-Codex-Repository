# 教学周日历与提醒系统（WebUI）

## 新增核心能力

- **首次启动管理员引导**：若系统中无管理员，自动跳转到 `/setup-admin` 创建管理员。
- **至少一个管理员约束**：删除管理员时会检查，系统始终至少保留一个管理员。
- **管理员用户管理**：管理员可创建/删除用户。
- **多用户数据隔离**：
  - 事件按用户独立保存；
  - 通知方式（Pushdeer、Graph）按用户独立保存；
  - 每周报告模板、发送时间按用户独立保存。
- **登录后才能操作**：未登录无法访问主页和所有操作接口。
- **按时间日志**：运行日志写入 `logs/app-YYYYMMDD.log`。

## Build / 运行

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python app.py
```

访问 `http://127.0.0.1:5000`。

- 第一次启动会进入管理员创建页。
- 创建管理员后，使用该账号登录。

## 邮件发送配置（Graph）

除了页面里的用户邮件配置外，也支持在 `config.yaml` 配置 `email` 段（优先级更高）：

```yaml
email:
  enable: true
  username: no-reply_xxx@outlook.com
  from_addr: no-reply_xxx@outlook.com
  auth_type: oauth2
  tenant_id: consumers
  authority: https://login.microsoftonline.com/consumers
  client_id: <your-client-id>
  client_secret: ""   # 留空时会走设备码登录
  scopes:
    - Mail.Send
    - User.Read
```

说明：
- `client_secret` 存在时优先走应用凭据；失败后回退设备码。
- `client_secret` 为空时直接走设备码（首次会在日志里打印登录提示）。
- `scopes` 可写 `Mail.Send` / `User.Read`，程序会自动补全为 Graph scope URL。
- MSAL 保留 scope（如 `offline_access` / `openid` / `profile`）会自动过滤，避免设备码流程报错。

## 说明

- 学期时间仍为全局配置（系统级）。
- 用户通知配置、报告配置、事件数据都是用户级隔离。
- 调度器任务运行在 `app.app_context()` 中，避免后台任务上下文报错。

## 邮件发送说明（你关心的两点）

1. **邮件发送到哪里？**
   - 默认发送到“用户注册邮箱” (`email_addr`)。
   - 管理员创建用户时必须填写注册邮箱。

2. **邮件 Subject 是否可自定义？**
   - 支持。每个用户都有自己的 `email_subject`（WebUI 可配置）。
   - 若 `config.yaml` 里配置了 `email.subject`，会优先使用该值。

