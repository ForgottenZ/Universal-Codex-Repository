#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
通用批量重命名（支持可变段数 & 模板）：
- 解析：
  1) 分隔符切分（默认 '-'，段数任意）
  2) 正则（可选，需包含命名组，例：(?P<a>...)-(?P<b>...)-(?P<c>...))
- 模板：
按住左键从勾选列拖动，所过之处批量设为同一状态（勾选或取消）。
  - 数字占位：{1},{2},{-1}（倒数第1），{3+}（第3段到末尾）
  - 命名占位：{a},{b},{c}（来自正则命名组）
  - 其他：{stem} 原始去扩展名，{ext} 扩展名（含点，支持多重扩展名），{name} 原始完整文件名
  - 过滤器：{2|upper|pad=3|strip|prefix=CH-|suffix=-END|replace=foo:bar}
  - 切片也可加过滤，如 {3+|lower}
- 默认不做任何变更（模板默认为 {stem}），命令行需显式 --apply 才执行。
- UI 模式：直接运行 python flex_renamer.py（无参数）
"""

from __future__ import annotations
import argparse, re, sys, uuid, json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any

# ------------------------- 工具函数 -------------------------

def all_suffixes(p: Path) -> str:
    return "".join(p.suffixes)

def real_stem(p: Path) -> str:
    suf = all_suffixes(p)
    name = p.name
    return name[:-len(suf)] if suf else name

def split_segments(stem: str, delim: str) -> List[str]:
    return stem.split(delim) if delim else [stem]

def safe_int(s: str, default: int) -> int:
    try:
        return int(s)
    except Exception:
        return default

# ------------------------- 配置与计划 -------------------------

@dataclass
class Rules:
    # 解析
    delimiter: str = "-"            # 分隔符模式（普通字符，不是正则）
    regex: Optional[str] = None     # 正则（命名组），可选
    recursive: bool = False

    # 过滤
    include: List[str] = field(default_factory=list)   # glob
    exclude: List[str] = field(default_factory=list)
    exts: List[str] = field(default_factory=list)      # .jpg .png .tar.gz

    # 模板与拼接
    template: str = "{stem}"        # 默认不变更
    slice_joiner: str = "-"         # {3+} 拼接时使用的连接符

    # 冲突策略
    conflict: str = "skip"          # skip | suffix
    suffix_sep: str = "_"           # 后缀分隔符，如 _1

    # 其他
    dry_run: bool = True
    preview_limit: int = 0
    export_plan: Optional[Path] = None

    # 排序与序号
    sort_key: str = "name"           # name | mtime | ctime
    sort_order: str = "asc"          # asc | desc
    seq_start: int = 1
    seq_step: int = 1
    seq_pad: int = 4

@dataclass
class PlanItem:
    src: Path
    dst: Path
    changed: bool
    reason: str

# ------------------------- 模板渲染引擎 -------------------------

_PLACEHOLDER = re.compile(r"""
\{
    (?P<key>[^|}]+)          # 键，如 1, -1, 3+, a, stem, ext
    (?:\|(?P<filters>[^}]+))? # 可选过滤器链，如 upper|pad=3
\}
""", re.VERBOSE)

def _apply_filters(val: str, filters: Optional[str]) -> str:
    if filters is None or filters.strip() == "":
        return val
    t = val
    for part in filters.split("|"):
        part = part.strip()
        if not part: 
            continue
        if part == "lower":
            t = t.lower()
        elif part == "upper":
            t = t.upper()
        elif part == "title":
            t = t.title()
        elif part == "strip":
            t = t.strip()
        elif part.startswith("pad=") or part.startswith("zfill="):
            n = safe_int(part.split("=",1)[1], 0)
            if n > 0 and t.isdigit():
                t = t.zfill(n)
        elif part.startswith("prefix="):
            t = part.split("=",1)[1] + t
        elif part.startswith("suffix="):
            t = t + part.split("=",1)[1]
        elif part.startswith("replace="):
            body = part.split("=",1)[1]
            if ":" in body:
                old, new = body.split(":",1)
                t = t.replace(old, new)
        else:
            # 未知过滤器：忽略
            pass
    return t

def _slice_join(items: List[str], joiner: str, filters: Optional[str]) -> str:
    # 对切片中每个元素应用过滤器，再连接
    proc = [_apply_filters(x, filters) for x in items]
    return joiner.join(proc)

def render_template(template: str, vars_map: Dict[str, Any], segs: List[str], joiner: str) -> str:
    """
    vars_map: 命名变量（正则命名组、stem/ext/name 等）
    segs: 分隔符切分后的段（1-based 可用）
    占位形式：
      {1} {2} {-1} {3+} {a} {stem} {ext} {name}
      可加过滤器管道：{2|upper|pad=3}
    """
    def repl(m: re.Match) -> str:
        key = m.group("key").strip()
        filters = m.group("filters")

        # 特殊键
        if key in vars_map:
            return _apply_filters(str(vars_map[key]), filters)

        # 数字/切片
        # {n}：1-based；{-1} 倒数第1；{n+}：n..末尾；{s:e} 切片（含端点）
        if key.endswith("+"):
            base = key[:-1]
            idx = safe_int(base, 0)
            if idx == 0:
                return ""
            if idx < 0:
                idx = len(segs) + 1 + idx
            start = max(1, idx)
            if start > len(segs):
                return ""
            return _slice_join(segs[start-1:], joiner, filters)
        elif ":" in key:
            # {start:end}（1-based；end 可为负）
            s_str, e_str = key.split(":",1)
            s = safe_int(s_str, 0); e = safe_int(e_str, 0)
            if s == 0 or e == 0:
                return ""
            if s < 0: s = len(segs) + 1 + s
            if e < 0: e = len(segs) + 1 + e
            s = max(1, s); e = min(len(segs), e)
            if s > e: 
                return ""
            return _slice_join(segs[s-1:e], joiner, filters)
        else:
            # 单个索引
            idx = safe_int(key, 0)
            if idx != 0:
                if idx < 0:
                    idx = len(segs) + 1 + idx
                if 1 <= idx <= len(segs):
                    return _apply_filters(segs[idx-1], filters)
                return ""
        # 未识别键
        return ""

    return _PLACEHOLDER.sub(repl, template)

# ------------------------- 解析与计划 -------------------------

def parse_with_regex(stem: str, pattern: str) -> Dict[str, str]:
    m = re.match(pattern, stem)
    return m.groupdict() if m else {}

def should_take(path: Path, rules: Rules) -> bool:
    if rules.exts:
        name = str(path).lower()
        if not any(name.endswith(ext.lower()) for ext in rules.exts):
            return False
    if rules.include:
        if not any(path.match(g) for g in rules.include):
            return False
    if rules.exclude:
        if any(path.match(g) for g in rules.exclude):
            return False
    return True

def collect_files(root: Path, recursive: bool) -> List[Path]:
    if recursive:
        return [p for p in root.rglob("*") if p.is_file()]
    return [p for p in root.iterdir() if p.is_file()]

def sort_files(files: List[Path], sort_key: str, sort_order: str) -> List[Path]:
    reverse = sort_order == "desc"
    if sort_key == "mtime":
        # 时间戳相同在批量图片中很常见（例如同一秒导出），
        # 增加文件名作为次级排序键，保证结果稳定、可预测。
        return sorted(files, key=lambda p: (p.stat().st_mtime, p.name.lower()), reverse=reverse)
    if sort_key == "ctime":
        return sorted(files, key=lambda p: (p.stat().st_ctime, p.name.lower()), reverse=reverse)
    return sorted(files, key=lambda p: p.name.lower(), reverse=reverse)

def build_plan(root: Path, rules: Rules) -> List[PlanItem]:
    files = sort_files(collect_files(root, rules.recursive), rules.sort_key, rules.sort_order)
    plan: List[PlanItem] = []
    seq_value = rules.seq_start
    for p in files:
        if p.name.startswith("__tmp_rename__"):
            continue
        if not should_take(p, rules):
            continue

        stem = real_stem(p)
        ext = all_suffixes(p)
        segs = split_segments(stem, rules.delimiter)
        vars_map: Dict[str, Any] = {
            "stem": stem,
            "ext": ext,
            "name": p.name,
            "seq": str(seq_value).zfill(max(0, rules.seq_pad)),
            "seq_raw": str(seq_value),
            "mtime": datetime.fromtimestamp(p.stat().st_mtime).isoformat(timespec="seconds"),
            "ctime": datetime.fromtimestamp(p.stat().st_ctime).isoformat(timespec="seconds")
        }
        seq_value += rules.seq_step
        if rules.regex:
            g = parse_with_regex(stem, rules.regex)
            vars_map.update(g)  # 命名变量

        # 渲染新名（如模板未包含 {ext}，则自动补回）
        new_stem = render_template(rules.template, vars_map, segs, rules.slice_joiner)
        if "{ext}" in rules.template:
            new_name = new_stem  # 模板自己放了 ext
        else:
            new_name = new_stem + ext

        changed = new_name != p.name
        dst = p.with_name(new_name)
        reason = "ok" if changed else "no-change"
        plan.append(PlanItem(src=p, dst=dst, changed=changed, reason=reason))

    if rules.preview_limit > 0:
        plan = plan[:rules.preview_limit]
    return plan

def dedupe(plan: List[PlanItem], rules: Rules) -> List[PlanItem]:
    if rules.conflict != "suffix":
        return plan
    seen = set()
    out: List[PlanItem] = []
    for it in plan:
        if not it.changed:
            out.append(it); continue
        cand = it.dst
        stem = real_stem(cand); ext = all_suffixes(cand)
        base = cand.name.lower()
        n = 0
        while base in seen:
            n += 1
            base = f"{stem}{rules.suffix_sep}{n}{ext}".lower()
        final = cand if n == 0 else cand.with_name(f"{stem}{rules.suffix_sep}{n}{ext}")
        seen.add(final.name.lower())
        out.append(PlanItem(src=it.src, dst=final, changed=(final.name != it.src.name), reason="dedup-suffix" if n>0 else it.reason))
    return out

def two_phase_rename(to_apply: List[PlanItem], root: Path) -> Tuple[int,int,List[str]]:
    tmp_tag = f"__tmp_rename__{uuid.uuid4().hex[:8]}__"
    temp_pairs: List[Tuple[Path,Path]] = []
    logs: List[str] = []
    ok = 0
    skipped = 0

    # 阶段1：源 -> 临时
    for it in to_apply:
        if not it.changed:
            skipped += 1
            logs.append(f"[跳过] 无变化：{it.src.name}")
            continue
        if it.dst.exists():
            skipped += 1
            logs.append(f"[跳过] 目标已存在：{it.src.name} -> {it.dst.name}")
            continue
        tmp = it.src.with_name(tmp_tag + it.src.name)
        try:
            it.src.rename(tmp)
            temp_pairs.append((tmp, it.dst))
        except Exception as e:
            skipped += 1
            logs.append(f"[失败] {it.src.name} -> {it.dst.name}（阶段1）：{e}")

    # 阶段2：临时 -> 目标
    for tmp, dst in temp_pairs:
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            tmp.rename(dst)
            ok += 1
            logs.append(f"[完成] {tmp.name.replace(tmp_tag,'',1)} -> {dst.name}")
        except Exception as e:
            logs.append(f"[失败] {tmp.name} -> {dst.name}（阶段2）：{e}")

    return ok, skipped, logs

def export_plan(plan: List[PlanItem], out_path: Path):
    if out_path.suffix.lower() == ".json":
        data = [{"src": str(it.src), "dst": str(it.dst), "changed": it.changed, "reason": it.reason} for it in plan]
        out_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    else:
        lines = ["src,dst,changed,reason"]
        for it in plan:
            lines.append(f"\"{it.src}\",\"{it.dst}\",\"{it.changed}\",\"{it.reason}\"")
        out_path.write_text("\n".join(lines), encoding="utf-8")

# ------------------------- CLI -------------------------

def run_cli(args):
    root = Path(args.path).resolve()
    if not root.exists() or not root.is_dir():
        print(f"路径不存在或不是目录：{root}", file=sys.stderr)
        sys.exit(2)

    rules = Rules(
        delimiter=args.delimiter,
        regex=args.regex,
        recursive=args.recursive,
        include=args.include or [],
        exclude=args.exclude or [],
        exts=args.exts or [],
        template=args.template,
        slice_joiner=args.slice_joiner or args.delimiter,
        conflict=args.conflict,
        suffix_sep=args.suffix_sep,
        dry_run=(not args.apply),
        preview_limit=args.preview_limit,
        export_plan=Path(args.export_plan) if args.export_plan else None,
        sort_key=args.sort_key,
        sort_order=args.sort_order,
        seq_start=args.seq_start,
        seq_step=args.seq_step,
        seq_pad=args.seq_pad
    )

    plan = build_plan(root, rules)
    if not plan:
        print("未找到可处理的文件或无变化。"); return
    plan = dedupe(plan, rules)

    # 预览
    print("预览 / 计划：")
    for it in plan:
        mark = "→" if it.changed else "·"
        print(f"{it.src.name}  {mark}  {it.dst.name}")
    print(f"\n合计 {len(plan)} 项（{sum(1 for it in plan if it.changed)} 项将变化）。")

    if rules.export_plan:
        export_plan(plan, rules.export_plan)
        print(f"已导出计划：{rules.export_plan}")

    if rules.dry_run:
        print("\n（预览模式）未执行重命名。使用 --apply 才会改名。")
        return

    to_apply = [it for it in plan if it.changed]
    ok, skipped, logs = two_phase_rename(to_apply, root)
    print("\n执行结果：")
    for line in logs:
        print(line)
    print(f"\n成功 {ok}，跳过/失败 {skipped}。")

# ------------------------- UI -------------------------

def run_ui():
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox

    win = tk.Tk()
    win.title("通用批量重命名（模板/可变段数）")
    win.geometry("980x640")

    state = {
        "root": Path(".").resolve(),
        "rules": Rules(),
        "plan": []  # type: List[PlanItem]
    ,
        "checked": set(),
        "drag": {"active": False, "target": None, "seen": set()},
    }

    def refresh_plan():
        try:
            r = state["rules"]
            state["plan"] = dedupe(build_plan(state["root"], r), r)
            tree.delete(*tree.get_children())
            changed = 0
            prev_names = set()
            for iid in list(state.get("checked", set())):
                try:
                    prev_names.add(state["plan"][int(iid)].src.name)
                except Exception:
                    pass
            state["checked"] = set()
            for i, it in enumerate(state["plan"]):
                status = "变化" if it.changed else "无变化"
                if it.changed:
                    changed += 1
                name = it.src.name
                checked = (name in prev_names) or it.changed
                if checked:
                    state["checked"].add(str(i))
                sel_symbol = "☑" if checked else "☐"
                tree.insert("", "end", iid=str(i), values=(sel_symbol, name, it.dst.name, status))
            lbl_count.config(text=f"共 {len(state['plan'])} 项，其中 {changed} 项将变化；已勾选 {len(state['checked'])} 项")
        except Exception as e:
            messagebox.showerror("错误", str(e))


    def choose_dir():
        d = filedialog.askdirectory(initialdir=str(state["root"]))
        if d:
            state["root"] = Path(d).resolve()
            ent_dir.delete(0, tk.END); ent_dir.insert(0, str(state["root"]))
            refresh_plan()

    def read_rules_from_ui():
        r = state["rules"]
        r.recursive = var_rec.get()
        r.delimiter = ent_delim.get()
        r.regex = ent_regex.get().strip() or None
        r.template = ent_tpl.get()
        r.slice_joiner = ent_join.get() or r.delimiter or "-"
        r.include = [s.strip() for s in ent_inc.get().split(",") if s.strip()]
        r.exclude = [s.strip() for s in ent_exc.get().split(",") if s.strip()]
        r.exts = [s.strip() for s in ent_exts.get().split(",") if s.strip()]
        r.conflict = cmb_conflict.get()
        r.suffix_sep = ent_suffix.get() or "_"
        r.sort_key = cmb_sort_key.get()
        r.sort_order = cmb_sort_order.get()
        r.seq_start = safe_int(ent_seq_start.get(), 1)
        r.seq_step = safe_int(ent_seq_step.get(), 1)
        r.seq_pad = safe_int(ent_seq_pad.get(), 4)

    def on_scan():
        read_rules_from_ui(); refresh_plan()

    def on_apply_selected():
        checked = list(state["checked"])
        if not checked:
            messagebox.showinfo("提示", "请勾选要改名的条目。"); return
        items = [state["plan"][int(i)] for i in checked if state["plan"][int(i)].changed]
        if not items:
            messagebox.showinfo("提示", "勾选的条目没有变化。"); return
        if not messagebox.askokcancel("确认", f"将对 {len(items)} 项执行重命名，是否继续？"):
            return
        ok, skipped, logs = two_phase_rename(items, state["root"])
        txt_logs.config(state="normal"); txt_logs.delete("1.0", tk.END); txt_logs.insert(tk.END, "\n".join(logs)); txt_logs.config(state="disabled")
        refresh_plan()
        messagebox.showinfo("完成", f"成功 {ok}，跳过/失败 {skipped}。")


    # 顶部
    frm_top = ttk.Frame(win); frm_top.pack(fill="x", padx=8, pady=6)
    ttk.Label(frm_top, text="目录：").pack(side="left")
    ent_dir = ttk.Entry(frm_top); ent_dir.pack(side="left", fill="x", expand=True)
    ent_dir.insert(0, str(state["root"]))
    ttk.Button(frm_top, text="选择…", command=choose_dir).pack(side="left", padx=4)
    ttk.Button(frm_top, text="扫描/预览", command=on_scan).pack(side="left", padx=4)
    ttk.Button(frm_top, text="对选中执行重命名", command=on_apply_selected).pack(side="left", padx=4)
    ttk.Button(frm_top, text="全选", command=lambda: [ _set_checked(i, True) for i in tree.get_children("") ]).pack(side="left", padx=2)
    ttk.Button(frm_top, text="全不选", command=lambda: [ _set_checked(i, False) for i in tree.get_children("") ]).pack(side="left", padx=2)
    ttk.Button(frm_top, text="反选", command=lambda: [ _set_checked(i, (i not in state["checked"])) for i in tree.get_children("") ]).pack(side="left", padx=2)

    # 解析/模板/过滤
    frm_opts = ttk.Frame(win); frm_opts.pack(fill="x", padx=8)

    frm_parse = ttk.LabelFrame(frm_opts, text="解析")
    frm_parse.pack(side="left", fill="both", expand=True, padx=4, pady=4)
    var_rec = tk.BooleanVar(value=False)
    ttk.Checkbutton(frm_parse, text="递归子目录", variable=var_rec).grid(row=0, column=0, sticky="w")
    ttk.Label(frm_parse, text="分隔符").grid(row=0, column=1, sticky="e")
    ent_delim = ttk.Entry(frm_parse, width=8); ent_delim.insert(0, "-"); ent_delim.grid(row=0, column=2, sticky="w")
    ttk.Label(frm_parse, text="正则（可选，含命名组 a,b,...）").grid(row=1, column=0, sticky="e", padx=4)
    ent_regex = ttk.Entry(frm_parse, width=40); ent_regex.grid(row=1, column=1, columnspan=2, sticky="we")

    frm_tpl = ttk.LabelFrame(frm_opts, text="模板")
    frm_tpl.pack(side="left", fill="both", expand=True, padx=4, pady=4)
    ttk.Label(frm_tpl, text="输出模板").grid(row=0, column=0, sticky="e")
    ent_tpl = ttk.Entry(frm_tpl, width=46); ent_tpl.insert(0, "{stem}"); ent_tpl.grid(row=0, column=1, sticky="we")
    ttk.Label(frm_tpl, text="切片连接符（{3+}）").grid(row=1, column=0, sticky="e")
    ent_join = ttk.Entry(frm_tpl, width=10); ent_join.insert(0, "-"); ent_join.grid(row=1, column=1, sticky="w")
    ttk.Label(frm_tpl, text="占位示例：{1} {2} {-1} {3+} {a} {stem} {ext} {seq}；过滤：|lower|upper|title|pad=3|strip|prefix=X-|suffix=-Y|replace=old:new").grid(row=2, column=0, columnspan=2, sticky="w", pady=2)

    frm_flt = ttk.LabelFrame(frm_opts, text="过滤")
    frm_flt.pack(side="left", fill="both", expand=True, padx=4, pady=4)
    ttk.Label(frm_flt, text="包含(glob, 逗号分隔)").grid(row=0, column=0, sticky="e")
    ent_inc = ttk.Entry(frm_flt, width=34); ent_inc.grid(row=0, column=1, sticky="we")
    ttk.Label(frm_flt, text="排除(glob)").grid(row=1, column=0, sticky="e")
    ent_exc = ttk.Entry(frm_flt, width=34); ent_exc.grid(row=1, column=1, sticky="we")
    ttk.Label(frm_flt, text="仅这些后缀(含点,逗号分隔)").grid(row=2, column=0, sticky="e")
    ent_exts = ttk.Entry(frm_flt, width=34); ent_exts.grid(row=2, column=1, sticky="we")
    ttk.Label(frm_flt, text="冲突策略").grid(row=3, column=0, sticky="e")
    cmb_conflict = ttk.Combobox(frm_flt, values=["skip","suffix"], state="readonly", width=8); cmb_conflict.set("skip"); cmb_conflict.grid(row=3, column=1, sticky="w")
    ttk.Label(frm_flt, text="后缀分隔符").grid(row=4, column=0, sticky="e")
    ent_suffix = ttk.Entry(frm_flt, width=8); ent_suffix.insert(0, "_"); ent_suffix.grid(row=4, column=1, sticky="w")
    ttk.Label(frm_flt, text="排序依据").grid(row=5, column=0, sticky="e")
    cmb_sort_key = ttk.Combobox(frm_flt, values=["name","mtime","ctime"], state="readonly", width=8); cmb_sort_key.set("name"); cmb_sort_key.grid(row=5, column=1, sticky="w")
    ttk.Label(frm_flt, text="排序顺序").grid(row=6, column=0, sticky="e")
    cmb_sort_order = ttk.Combobox(frm_flt, values=["asc","desc"], state="readonly", width=8); cmb_sort_order.set("asc"); cmb_sort_order.grid(row=6, column=1, sticky="w")
    ttk.Label(frm_flt, text="序号起始/步长/位数").grid(row=7, column=0, sticky="e")
    frm_seq = ttk.Frame(frm_flt); frm_seq.grid(row=7, column=1, sticky="w")
    ent_seq_start = ttk.Entry(frm_seq, width=5); ent_seq_start.insert(0, "1"); ent_seq_start.pack(side="left")
    ttk.Label(frm_seq, text="/").pack(side="left")
    ent_seq_step = ttk.Entry(frm_seq, width=5); ent_seq_step.insert(0, "1"); ent_seq_step.pack(side="left")
    ttk.Label(frm_seq, text="/").pack(side="left")
    ent_seq_pad = ttk.Entry(frm_seq, width=5); ent_seq_pad.insert(0, "4"); ent_seq_pad.pack(side="left")

    # 表格与日志
    frm_tbl = ttk.Frame(win); frm_tbl.pack(fill="both", expand=True, padx=8, pady=6)
    columns = ("sel","src","dst","status")
    tree = ttk.Treeview(frm_tbl, columns=columns, show="headings", selectmode="none")
    for col, w, anch in [("sel",46,"center"),("src",420,"w"),("dst",420,"w"),("status",90,"w")]:
        tree.heading(col, text={"sel":"选","src":"原名","dst":"新名","status":"状态"}[col])
        tree.column(col, width=w, anchor=anch)
    tree.pack(fill="both", expand=True)

    def _set_checked(iid: str, flag: bool):
        if flag:
            state["checked"].add(iid)
        else:
            state["checked"].discard(iid)
        tree.set(iid, "sel", "☑" if flag else "☐")
        base = f"共 {len(state['plan'])} 项，其中 " + str(sum(1 for it in state["plan"] if it.changed)) + " 项将变化"
        lbl_count.config(text=base + f"；已勾选 {len(state['checked'])} 项")

    def on_click(e):
        col = tree.identify_column(e.x)
        row = tree.identify_row(e.y)
        if col == "#1" and row:
            new_state = not (row in state["checked"])
            _set_checked(row, new_state)
            state["drag"] = {"active": True, "target": new_state, "seen": {row}}
            return "break"

    def on_drag(e):
        if not state["drag"]["active"]:
            return
        row = tree.identify_row(e.y)
        if row and row not in state["drag"]["seen"]:
            _set_checked(row, state["drag"]["target"])
            state["drag"]["seen"].add(row)

    def on_release(e):
        state["drag"] = {"active": False, "target": None, "seen": set()}

    tree.bind("<Button-1>", on_click)
    tree.bind("<B1-Motion>", on_drag)
    tree.bind("<ButtonRelease-1>", on_release)

    lbl_count = ttk.Label(win, text="共 0 项"); lbl_count.pack(anchor="w", padx=10)

    frm_log = ttk.LabelFrame(win, text="日志"); frm_log.pack(fill="both", padx=8, pady=4)
    txt_logs = tk.Text(frm_log, height=6, state="disabled")
    txt_logs.pack(fill="both", expand=True)

    refresh_plan()
    win.mainloop()

# ------------------------- 入口 -------------------------

def main():
    # 无任何参数时启动 UI；有参数则走 CLI
    if len(sys.argv) == 1:
        run_ui()
        return

    p = argparse.ArgumentParser(description="通用批量重命名（可变段数 & 模板 & UI）")
    p.add_argument("path", nargs="?", default=".", help="目标目录（默认当前目录）")
    p.add_argument("--recursive", action="store_true", help="递归子目录")

    # 解析
    p.add_argument("--delimiter", default="-", help="分隔符（默认 '-'）")
    p.add_argument("--regex", default=None, help="正则解析（含命名组），如 '^(?P<a>[^-]+)-(?P<b>[^-]+)-(?P<c>.+)$'")

    # 模板
    p.add_argument("--template", default="{stem}",
                   help="输出模板，例：'{2}-{1}'、'{a}-{b}'、'frame_{seq}'、'{1}-{2}{ext}'；若模板未含 {ext} 将自动追加原扩展名")
    p.add_argument("--slice-joiner", default=None, help="切片连接符（{3+} 时使用；默认用分隔符）")

    # 过滤/冲突
    p.add_argument("--include", nargs="*", help="仅处理这些 glob（空格分隔）")
    p.add_argument("--exclude", nargs="*", help="排除这些 glob")
    p.add_argument("--exts", nargs="*", help="仅处理这些后缀（含点），例：.jpg .png .tar.gz")
    p.add_argument("--conflict", default="skip", choices=["skip","suffix"], help="冲突策略：skip 跳过，suffix 自动加序号")
    p.add_argument("--suffix-sep", default="_", help="自动序号分隔符，默认 '_'")
    p.add_argument("--sort-key", default="name", choices=["name","mtime","ctime"], help="排序依据：name 文件名，mtime 修改时间，ctime 创建/元数据变更时间")
    p.add_argument("--sort-order", default="asc", choices=["asc","desc"], help="排序顺序：asc 升序，desc 降序")
    p.add_argument("--seq-start", type=int, default=1, help="{seq} 起始值，默认 1")
    p.add_argument("--seq-step", type=int, default=1, help="{seq} 步长，默认 1")
    p.add_argument("--seq-pad", type=int, default=4, help="{seq} 补零位数，默认 4（例如 0001）")

    # 执行/导出
    p.add_argument("--apply", action="store_true", help="实际执行改名（默认仅预览）")
    p.add_argument("--export-plan", help="导出计划到 .csv 或 .json")
    p.add_argument("--preview-limit", type=int, default=0, help="预览数量上限（0 不限）")

    args = p.parse_args()
    run_cli(args)

if __name__ == "__main__":
    main()
