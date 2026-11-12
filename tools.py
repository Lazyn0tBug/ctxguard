#!/usr/bin/env python3
"""ctxguard 工具层：tree / outline / read / search / symbols / overview。CLI、MCP、hook 共用。仅标准库。"""
import bisect
import fnmatch
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from typing import Any, TypeIs

import lib
from lib import (
    BIN_EXTS,
    LINE_CLIP,
    big_file_message,
    build,
    chunks_for,
    est_tokens,
    open_db,
    outline_text,
    read_lines,
)


def like_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


# ---------------------------------------------------------------- 根目录 / 配置
def find_root(start: str | None = None) -> str:
    r = os.environ.get("CTXGUARD_ROOT")
    if r:
        return os.path.abspath(r)
    start = os.path.abspath(start or os.getcwd())
    try:
        out = subprocess.run(["git", "-C", start, "rev-parse", "--show-toplevel"], capture_output=True, timeout=10)
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.decode("utf-8", "replace").strip()
    except Exception:
        pass
    return start


@dataclass(slots=True)
class Config:
    """生效中的配置。每一项都经过校验：写坏的项退回默认并警告。

    不用 pydantic：ctxguard 靠 install.py 复制散装 .py 投放、hook 用系统 python3 直接跑，
    任何必需的第三方依赖都会让没装包的用户机器直接失效——而 hook 是 fail-open，
    失效等于静默放行，读约束整个消失。
    """

    max_read_tok: int = 4000              # 单次读取上限（估算 tokens），超过即拒绝
    session_budget: int = 100000          # 主线程累计读取预算，0=关闭
    allow: list[str] = field(default_factory=list)   # 允许整份读取的路径 glob（相对项目根）
    grep_content_max: int = 60            # 内置 Grep content 模式允许的 head_limit 上限
    bash_guard: bool = True
    backends: list[str] = field(default_factory=list)  # 声明提取后端；空=只用启发式
    backend_options: dict[str, Any] = field(default_factory=dict)  # descend_functions、max_depth


CONFIG_WARNINGS: list[str] = []          # 配置校验产生的警告（SessionStart 会回显）


def _is_dict(v: object) -> TypeIs[dict[str, Any]]:
    """窄化：JSON 顶层不是对象就当空配置。"""
    return isinstance(v, dict)


def _warn(name: str, value: object, expect: str) -> None:
    """配置项非法：退回默认值并警告。配置写错不该让 hook 静默失效。"""
    msg = "[ctxguard] 配置项 %s 期望%s，实际 %r —— 已退回默认值" % (name, expect, value)
    sys.stderr.write(msg + "\n")
    CONFIG_WARNINGS.append(msg)


def _as_int(value: object, default: int, name: str) -> int:
    """取整数配置项；类型不对或转不了就退回默认并警告。"""
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        _warn(name, value, "整数")
        return default
    try:
        return int(value)
    except ValueError:
        _warn(name, value, "整数")
        return default


def _as_bool(value: object, default: bool, name: str) -> bool:
    """取布尔配置项。只认真正的布尔与常见字符串写法——bool("false") 是 True，不能直接转。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.lower() in ("true", "false", "on", "off", "1", "0", "yes", "no"):
        return value.lower() in ("true", "on", "1", "yes")
    _warn(name, value, "布尔值")
    return default


def _as_str_list(value: object, default: list[str], name: str) -> list[str]:
    """取字符串数组配置项；单个字符串按逗号切分（方便手写 .ctxguard.json）。"""
    if isinstance(value, str):
        return [x.strip() for x in value.split(",") if x.strip()]
    if isinstance(value, list) and all(isinstance(x, str) for x in value):
        return list(value)
    _warn(name, value, "字符串数组")
    return default


def _read_json(p: str) -> dict[str, Any]:
    try:
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
        return d if _is_dict(d) else {}
    except Exception:
        return {}


def load_config(root: str) -> Config:
    """读配置并逐项校验。

    优先级：环境变量 > 项目 .ctxguard.json > 用户 ~/.ctxguard.json > 默认。
    安全：项目文件里的 backends 只允许内置后端名；任意模块/plugin: 只接受用户级配置或环境变量。
    写坏的项退回默认并警告——不让一个手误的配置把 hook 变成“什么都不拦”。
    """
    import backends
    user = _read_json(os.path.expanduser("~/.ctxguard.json"))
    proj = _read_json(os.path.join(root, ".ctxguard.json"))
    cfg = Config(
        max_read_tok=lib.MAX_READ_TOK,                                  # 已含 CTXGUARD_MAX_READ_TOK
        session_budget=int(os.environ.get("CTXGUARD_SESSION_BUDGET", "100000")),
        bash_guard=os.environ.get("CTXGUARD_BASH", "on") != "off",
    )
    for src, tag in ((user, "用户"), (proj, "项目")):
        if "max_read_tok" in src:
            cfg.max_read_tok = _as_int(src["max_read_tok"], cfg.max_read_tok, tag + " max_read_tok")
        if "session_budget" in src:
            cfg.session_budget = _as_int(src["session_budget"], cfg.session_budget, tag + " session_budget")
        if "grep_content_max" in src:
            cfg.grep_content_max = _as_int(src["grep_content_max"], cfg.grep_content_max, tag + " grep_content_max")
        if "bash_guard" in src:
            cfg.bash_guard = _as_bool(src["bash_guard"], cfg.bash_guard, tag + " bash_guard")
        if "allow" in src:
            cfg.allow = _as_str_list(src["allow"], cfg.allow, tag + " allow")
        bo = src.get("backend_options")
        if _is_dict(bo):
            if "descend_functions" in bo:
                cfg.backend_options["descend_functions"] = _as_bool(
                    bo["descend_functions"], False, tag + " backend_options.descend_functions")
            if "max_depth" in bo:
                cfg.backend_options["max_depth"] = _as_int(
                    bo["max_depth"], 6, tag + " backend_options.max_depth")

    ignored = []
    env = os.environ.get("CTXGUARD_BACKENDS")
    if env is not None:
        names = backends.expand(env)
    elif "backends" in proj:
        names = []
        for n in backends.expand(proj["backends"]):
            if backends.is_builtin(n):
                names.append(n)
            else:
                ignored.append(n)
    else:
        names = backends.expand(user.get("backends"))
    cfg.backends = names

    lib.MAX_READ_TOK = cfg.max_read_tok
    lib.BACKENDS = names
    lib.BACKEND_IGNORED = ignored
    lib.BACKEND_OPTS = {"descend_functions": bool(cfg.backend_options.get("descend_functions", False)),
                        "max_depth": int(cfg.backend_options.get("max_depth", 6))}
    backends.LOG_PATH = os.path.join(root, lib.INDEX_DIR, "backend.log")
    lib.CONFIG_LOADED = True
    return cfg


def resolve(root: str, path: str | None) -> tuple[str, str]:
    path = path or "."
    ab = os.path.normpath(path if os.path.isabs(path) else os.path.join(root, path))
    try:
        rel = os.path.relpath(ab, root).replace(os.sep, "/")
    except ValueError:
        rel = ab
    if rel.startswith(".."):
        rel = ab
    return ab, rel


def file_tokens(path: str) -> int:
    try:
        size = os.path.getsize(path)
    except OSError:
        return 0
    if size > lib.MAX_INDEX_BYTES:
        return int(size / 3.5)
    with open(path, "rb") as f:
        data = f.read()
    if b"\0" in data[:8192]:
        return int(size / 3.5)
    return est_tokens(data.decode("utf-8", "replace"))


def lines_tokens(lines: list[str]) -> int:
    return est_tokens("\n".join(ln[:LINE_CLIP] for ln in lines))


def refresh(root: str, limit: int = 15) -> dict[str, int | bool]:
    return build(root, time_limit=limit)


LONG_LINE = 3000


def longline_view(root: str, ab: str) -> str | None:
    """文件含超长行（压缩 JSON/JS 等）时按行无法导航：生成一份可按行读取的视图，返回其绝对路径；否则返回 None。"""
    try:
        if os.path.getsize(ab) > lib.MAX_INDEX_BYTES or os.path.splitext(ab)[1].lower() in BIN_EXTS:
            return None
        if os.path.getsize(ab) <= LONG_LINE:
            return None
        with open(ab, "rb") as f:
            data = f.read()
    except OSError:
        return None
    if b"\0" in data[:8192]:
        return None
    text = data.decode("utf-8", "replace")
    if max((len(ln) for ln in text.split("\n")), default=0) <= LONG_LINE:
        return None
    _, rel = resolve(root, ab)
    vdir = os.path.join(root, lib.INDEX_DIR, "views")
    os.makedirs(vdir, exist_ok=True)
    vp = os.path.join(vdir, re.sub(r"[^\w.\-]", "_", rel) + ".view")
    if os.path.exists(vp) and os.path.getmtime(vp) >= os.path.getmtime(ab):
        return vp
    try:
        body = json.dumps(json.loads(text), ensure_ascii=False, indent=1)
    except Exception:
        body = re.sub(r"([;{}])", r"\1\n", text)
    out = []
    for ln in body.split("\n"):
        while len(ln) > 200:
            out.append(ln[:200])
            ln = ln[200:]
        out.append(ln)
    with open(vp, "w", encoding="utf-8") as f:
        f.write("\n".join(out) + "\n")
    return vp


def big_message(root: str, ab: str, what: str) -> str:
    """拒绝整份读取时给模型的反馈：目录 + 下一步；含超长行的文件改指向可读视图。"""
    v = longline_view(root, ab)
    if v:
        _, rel = resolve(root, ab)
        vrel = os.path.relpath(v, root).replace(os.sep, "/")
        return (f"[ctxguard] 拒绝：{rel} 含超长行（压缩的 JSON/JS 等），无法按行读取。已生成可读视图 {vrel}"
                f"（内容等价，已格式化）。请对该视图用 outline / Read(offset,limit) / read(start,end) 分段读取，"
                f"或 search 定位。视图目录：\n" + outline_text(chunks_for(root, v), 40))
    return big_file_message(root, ab, what)


# ---------------------------------------------------------------- tree
def t_tree(root: str, path: str = ".", depth: int = 2, maxrows: int = 100) -> str:
    ab, rel = resolve(root, path)
    if os.path.isfile(ab):
        return t_outline(root, path)
    if not os.path.isdir(ab):
        return f"ERROR: 路径不存在：{path}"
    refresh(root)
    prefix = "" if rel in (".", "") else rel.rstrip("/") + "/"
    con = open_db(root)
    rows = [lib.TreeRow(*r) for r in
            con.execute("SELECT path,kind,tok,nchunks FROM files WHERE path LIKE ? ESCAPE '\\' ORDER BY path",
                        (like_escape(prefix) + "%",)).fetchall()]
    con.close()
    if not rows:
        return f"(没有已索引的文件：{rel})"
    d = max(1, int(depth or 2))
    while True:
        ents = {}
        for r in rows:
            p, kind, tok, nch = r.path, r.kind, r.tok, r.nchunks
            parts = p.removeprefix(prefix).split("/")
            if len(parts) > d:
                key, isdir = "/".join(parts[:d]) + "/", True
            else:
                key, isdir = "/".join(parts), False
            e = ents.setdefault(key, [isdir, 0, 0, kind, nch])
            e[1] += 1
            e[2] += tok
        if len(ents) <= maxrows or d == 1:
            break
        d -= 1
    items = sorted(ents.items(), key=lambda kv: (not kv[1][0], kv[0]))
    total_tok = sum(r.tok for r in rows)
    out = [f"{rel if prefix else '.'}/ | {len(rows)} 个文件 | ~{total_tok}tok（展开层级={d}）"]
    for key, (isdir, n, tok, kind, nch) in items[:maxrows]:
        if isdir:
            out.append(f"  {key} | {n} 文件 | ~{tok}tok")
        else:
            extra = "" if kind in ("code", "doc") else f" | {kind}"
            out.append(f"  {key} | ~{tok}tok | {nch}段{extra}")
    if len(items) > maxrows:
        out.append(f"  …… 另有 {len(items) - maxrows} 项：请缩小 path")
    out.append("下一步：对感兴趣的目录再 tree(path)，对文件 outline(path)。")
    return "\n".join(out)


# ---------------------------------------------------------------- outline
def t_outline(root: str, path: str) -> str:
    ab, rel = resolve(root, path)
    if os.path.isdir(ab):
        return t_tree(root, path)
    if not os.path.isfile(ab):
        return f"ERROR: 路径不存在：{path}"
    ext = os.path.splitext(ab)[1].lower()
    if ext in BIN_EXTS:
        return f"{rel}: 二进制文件（{os.path.getsize(ab)} 字节），不提供文本目录"
    v = longline_view(root, ab)
    note = None
    if v:
        note = f"{rel} 含超长行（压缩的 JSON/JS 等），按行无法导航；以下是已格式化的可读视图 {os.path.relpath(v, root).replace(os.sep, '/')} 的目录，之后请对该视图 read/search："
        ab, rel = v, os.path.relpath(v, root).replace(os.sep, "/")
    rows = chunks_for(root, ab)
    ttok = file_tokens(ab)
    nl = rows[-1].e if rows else 0
    out = ([note] if note else []) + [f"{rel} | {nl} 行 | ~{ttok}tok | {len(rows)} 段"]
    if ttok <= lib.MAX_READ_TOK:
        out.append("（文件较小，可以直接 read 整份）")
    if rows:
        out.append("起止行 | tokens | 标题 — 摘要")
        out.append(outline_text(rows, 80))
    out.append("下一步：read(path, start, end) 只读需要的一段。")
    return "\n".join(out)


# ---------------------------------------------------------------- read
def t_read(root: str, path: str, start: int | None = None, end: int | None = None) -> str:
    ab, rel = resolve(root, path)
    if not os.path.isfile(ab):
        return f"ERROR: 文件不存在：{path}"
    if os.path.splitext(ab)[1].lower() in BIN_EXTS:
        return f"ERROR: {rel} 是二进制文件，不能按文本读取"
    v = longline_view(root, ab)
    if v:
        return "REFUSED: " + big_message(root, ab, "")
    lines = read_lines(ab)
    total = len(lines)
    if total == 0:
        return f"{rel}: (空文件)"
    if start is None and end is None:
        if lines_tokens(lines) > lib.MAX_READ_TOK:
            return "REFUSED: " + big_message(root, ab, "整份读取")
        s, e = 1, total
    else:
        s = max(1, int(start or 1))
        e = min(total, int(end) if end is not None else s + 199)
        if s > total:
            return f"ERROR: 起始行 {s} 超过文件总行数 {total}"
        e = max(e, s)
    seg = lines[s - 1:e]
    tok = lines_tokens(seg)
    if tok > lib.MAX_READ_TOK:
        rows = [r for r in chunks_for(root, ab) if r.e >= s and r.s <= e]
        return (f"REFUSED: 区间 {s}-{e}（约 {tok}tok）超过单次上限 {lib.MAX_READ_TOK}tok。"
                f"请缩小区间，或按下面的段分别读取：\n" + outline_text(rows, 40))
    body = "\n".join(
        f"{i}\t{ln if len(ln) <= LINE_CLIP else ln[:LINE_CLIP] + '…[+%d字符]' % (len(ln) - LINE_CLIP)}"
        for i, ln in enumerate(seg, s))
    foot = ""
    if e < total:
        nxt = next((r for r in chunks_for(root, ab) if r.s <= e + 1 <= r.e), None)
        foot = (f"\n[未读完：下一段 {e + 1}-{nxt.e} {nxt.title[:60]}]" if nxt
                else f"\n[未读完：下一步 read(start={e + 1})]")
    return f"[{rel} 行 {s}-{e} / 共 {total} 行 | ~{tok}tok]\n{body}{foot}"


# ---------------------------------------------------------------- search
def _hit_chunks(root, full, hl, top=4):
    rows = chunks_for(root, full)
    if not rows:
        return ""
    starts = [c.s for c in rows]
    cnt = {}
    for ln, _ in hl:
        k = bisect.bisect_right(starts, ln) - 1
        if k >= 0:
            cnt[k] = cnt.get(k, 0) + 1
    items = sorted(cnt.items(), key=lambda kv: (-kv[1], kv[0]))[:top]
    items.sort()
    return "; ".join(f"{rows[k].s}-{rows[k].e} {rows[k].title[:50]}({n})" for k, n in items)


def t_search(root: str, pattern: str, path: str = ".", mode: str = "auto", limit: int = 40,
             glob: str | None = None) -> str:
    if not pattern:
        return "ERROR: pattern 为空"
    ab, rel = resolve(root, path)
    flags = 0 if any(c.isupper() for c in pattern) else re.I
    try:
        rx = re.compile(pattern, flags)
    except re.error:
        rx = re.compile(re.escape(pattern), flags)
    if os.path.isfile(ab):
        scope = [rel]
    elif os.path.isdir(ab):
        refresh(root)
        prefix = "" if rel in (".", "") else rel.rstrip("/") + "/"
        con = open_db(root)
        scope = [r[0] for r in con.execute(
            "SELECT path FROM files WHERE path LIKE ? ESCAPE '\\' AND kind IN ('code','doc') AND size<=? ORDER BY path",
            (like_escape(prefix) + "%", 2_000_000))]
        con.close()
    else:
        return f"ERROR: 路径不存在：{path}"
    if glob:
        scope = [p for p in scope if fnmatch.fnmatch(p, glob) or fnmatch.fnmatch(os.path.basename(p), glob)]
    t0, hits, timed_out = time.monotonic(), {}, False
    for p in scope:
        if time.monotonic() - t0 > 15:
            timed_out = True
            break
        full = os.path.join(root, p)
        try:
            lines = read_lines(full)
        except OSError:
            continue
        hl = []
        for i, ln in enumerate(lines, 1):
            if rx.search(ln[:2000]):
                hl.append((i, ln))
                if len(hl) >= 300:
                    break
        if hl:
            hits[p] = hl
    if not hits:
        return f"无匹配（已搜索 {len(scope)} 个文件{'，超时未搜完' if timed_out else ''}）"
    nfiles, total = len(hits), sum(len(v) for v in hits.values())
    limit = max(1, min(int(limit or 40), 100))
    if mode == "auto":
        mode = "lines" if (nfiles <= 3 and total <= limit) else "files"
    out = [f"{total} 处匹配，分布在 {nfiles} 个文件（范围 {len(scope)} 个文件{'，超时未搜完' if timed_out else ''}）"]
    if mode == "lines":
        shown = 0
        for p in sorted(hits):
            out.append(p)
            for ln, line in hits[p]:
                if shown >= limit:
                    break
                out.append(f"  {ln}: {' '.join(line.split())[:160]}")
                shown += 1
            if shown >= limit:
                out.append(f"  …… 已显示 {shown}/{total} 行，请缩小 path 或换更精确的 pattern")
                break
        out.append("下一步：read(path, start, end) 读命中附近。")
    else:
        ranked = sorted(hits.items(), key=lambda kv: (-len(kv[1]), kv[0]))
        for p, hl in ranked[:30]:
            out.append(f"{p} | {len(hl)}处 | 段: {_hit_chunks(root, os.path.join(root, p), hl)}")
        if len(ranked) > 30:
            out.append(f"…… 另有 {len(ranked) - 30} 个文件")
        out.append("下一步：缩小 path 到单个文件，或 mode=lines 查看具体行（受 limit 约束）。")
    return "\n".join(out)


# ---------------------------------------------------------------- symbols
def t_symbols(root: str, query: str = "", path: str | None = None, limit: int = 50) -> str:
    refresh(root)
    limit = max(1, min(int(limit or 50), 200))
    con = open_db(root)
    where, args = [], []
    if path:
        ab, rel = resolve(root, path)
        if os.path.isfile(ab):
            where.append("path=?")
            args.append(rel)
        elif rel not in (".", ""):
            where.append("path LIKE ? ESCAPE '\\'")
            args.append(like_escape(rel.rstrip("/") + "/") + "%")
    if query:
        where.append("name LIKE ? ESCAPE '\\'")
        args.append("%" + like_escape(query) + "%")
    sql = "SELECT path,line,name FROM symbols" + (" WHERE " + " AND ".join(where) if where else "")
    rows = con.execute(sql + " ORDER BY path,line LIMIT ?", args + [limit + 1]).fetchall()
    con.close()
    if not rows:
        return "无匹配符号"
    out = [f"{p}:{ln} | {nm}" for p, ln, nm in rows[:limit]]
    if len(rows) > limit:
        out.append(f"…… 结果超过 {limit} 条，请加 path 或更精确的 query")
    out.append("下一步：read(path, start=行号, end=行号+60)。")
    return "\n".join(out)


# ---------------------------------------------------------------- overview（SessionStart 注入）
def overview(root: str, maxrows: int = 25) -> str:
    out = t_tree(root, ".", depth=1, maxrows=maxrows).split("\n")
    return "\n".join(out[:-1])
