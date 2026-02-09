# work

一个面向普通用户的**批量重命名工具**（Python，支持命令行 + 图形界面）。

你可以把它当成“文件名模板引擎”：
- 先决定按什么顺序处理文件（例如按时间从旧到新）；
- 再写一个输出模板（例如 `frame_{seq}`）；
- 工具就会按顺序生成新名字（例如 `frame_0001`、`frame_0002`、`frame_0003`）。

---

## 1. 快速开始

### 图形界面（推荐新手）
```bash
python 批量重命名.py
```

### 命令行（可自动化）
```bash
python 批量重命名.py 目标目录 --template "frame_{seq}" --sort-key mtime --sort-order asc
```

> 默认是预览模式，不会真的改名。要实际执行，请加 `--apply`。

---

## 2. 你最关心的新功能

### A) 按文件时间排序
- `--sort-key mtime`：按修改时间排序。
- `--sort-key ctime`：按创建/元数据变更时间排序（跨平台含义略有差异）。
- `--sort-order asc|desc`：升序/降序。

### B) 数字递增占位
- 新变量：`{seq}`（带补零）与 `{seq_raw}`（不补零）。
- 配套参数：
  - `--seq-start` 起始值（默认 1）
  - `--seq-step` 步长（默认 1）
  - `--seq-pad` 补零位数（默认 4）

#### 你的例子（A 最新，C 最旧）
从旧到新、模板 `frame_{seq}`：
```bash
python 批量重命名.py ./files \
  --template "frame_{seq}" \
  --sort-key mtime \
  --sort-order asc \
  --seq-start 1 --seq-step 1 --seq-pad 4 \
  --apply
```
结果将是：
- C -> `frame_0001`
- B -> `frame_0002`
- A -> `frame_0003`

---

## 3. 模板变量（重新整理版）

### 常用变量
- `{stem}`：原文件名（不含扩展名）
- `{ext}`：扩展名（含点，支持多后缀）
- `{name}`：原完整文件名
- `{seq}`：递增序号（自动补零）
- `{seq_raw}`：递增序号（不补零）
- `{mtime}` / `{ctime}`：文件时间（ISO 格式）

### 按分隔符取段
假设文件名 `A-B-C.jpg`，分隔符是 `-`：
- `{1}` => `A`
- `{2}` => `B`
- `{-1}` => `C`
- `{2+}` => `B-C`
- `{1:2}` => `A-B`

### 正则命名组
若 `--regex '^(?P<prefix>[^-]+)-(?P<id>\d+)$'`，模板可用 `{prefix}`、`{id}`。

### 过滤器（可链式）
示例：`{2|upper|prefix=ID-}`
- `lower` / `upper` / `title`
- `strip`
- `pad=4`（或 `zfill=4`）
- `prefix=xxx`
- `suffix=xxx`
- `replace=old:new`

---

## 4. 常见命令示例

### 仅预览（安全）
```bash
python 批量重命名.py ./files --template "{stem}_{seq}"
```

### 只处理 png/jpg，并且递归子目录
```bash
python 批量重命名.py ./files --recursive --exts .png .jpg --template "img_{seq}" --apply
```

### 文件名冲突时自动补后缀
```bash
python 批量重命名.py ./files --template "frame_{seq}" --conflict suffix --suffix-sep _ --apply
```

### 导出计划到 JSON/CSV
```bash
python 批量重命名.py ./files --template "frame_{seq}" --export-plan plan.json
```

---

## 5. 我额外补充的实用能力（本次整理说明）

除了你要求的“按时间排序 + 数字递增占位”，还做了这些增强：
1. **UI 中也可设置排序与序号规则**（不用只靠命令行）。
2. **新增模板变量 `seq_raw`**，用于不补零的流水号场景。
3. **新增 `mtime/ctime` 模板变量**，便于把时间直接拼进文件名。
4. **修复 UI 勾选默认逻辑中的硬编码异常条件**，避免困惑。

---

## 6. 安全建议

- 先不加 `--apply` 预览。
- 大批量改名前先备份。
- 对关键目录先用 `--preview-limit` 做小样本验证。
