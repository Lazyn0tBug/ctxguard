#!/usr/bin/env python3
"""ctxguard 工具层：tree / outline / read / search / symbols / overview。CLI、MCP、hook 共用。仅标准库。"""
import bisect
import fnmatch
import json
import os
import re
import subprocess
import time

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


def like_escape(s):
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


# ---------------------------------------------------------------- 根目录 / 配置
def find_root(start=None):
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


def _read_json(p):
    try:
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def load_config(root):
    """配置优先级：环境变量 > 项目 .ctxguard.json > 用户 ~/.ctxguard.json > 默认。
    安全：项目文件里的 backends 只允许内置后端名；任意模块/plugin: 只接受用户级配置或环境变量。"""
    import backends
    cfg = {
        "max_read_tok": lib.MAX_READ_TOK,   # 单次读取上限（估算 tokens），超过即拒绝
        "session_budget": int(os.environ.get("CTXGUARD_SESSION_BUDGET", "100000")),  # 主线程累计读取预算，0=关闭
        "allow": [],                        # 允许整份读取的路径 glob（相对项目根）
        "grep_content_max": 60,             # 内置 Grep content 模式允许的 head_limit 上限
        "bash_guard": os.environ.get("CTXGUARD_BASH", "on") != "off",
        "backends": [],                     # 声明提取后端，如 ["treesitter", "pyast"] 或 "auto"；空=只用启发式
        "backend_options": {},              # descend_functions(bool)、max_depth(int)
    }
    user = _read_json(os.path.expanduser("~/.ctxguard.json"))
    proj = _read_json(os.path.join(root, ".ctxguard.json"))
    # 用 update 而不是 |=：|= 会把值类型并集化（并进 JSON 里的 list/dict），
    # update 保持 cfg 自身的推断类型，ty 才不会被 cfg["max_read_tok"] 的类型搅浑。
    cfg.update({k: v for k, v in user.items() if k != "backends"})
    cfg.update({k: v for k, v in proj.items() if k != "backends"})
    opts = {}
    for src in (user, proj):
        bo = src.get("backend_options")
        if isinstance(bo, dict):
            opts.update(bo)
    cfg["backend_options"] = opts

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
    cfg["backends"] = names

    lib.MAX_READ_TOK = int(cfg["max_read_tok"])
    lib.BACKENDS = names
    lib.BACKEND_IGNORED = ignored
    lib.BACKEND_OPTS = {"descend_functions": bool(opts.get("descend_functions", False)),
                        "max_depth": int(opts.get("max_depth", 6))}
    backends.LOG_PATH = os.path.join(root, lib.INDEX_DIR, "backend.log")
    lib.CONFIG_LOADED = True
    return cfg


def resolve(root, path):
    path = path or "."
    ab = os.path.normpath(path if os.path.isabs(path) else os.path.join(root, path))
    try:
        rel = os.path.relpath(ab, root).replace(os.sep, "/")
    except ValueError:
        rel = ab
    if rel.startswith(".."):
        rel = ab
    return ab, rel


def file_tokens(path):
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


def lines_tokens(lines):
    return est_tokens("\n".join(ln[:LINE_CLIP] for ln in lines))


def refresh(root, limit=15):
    return build(root, time_limit=limit)


LONG_LINE = 3000


def longline_view(root, ab):
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


def big_message(root, ab, what):
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
def t_tree(root, path=".", depth=2, maxrows=100):
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
def t_outline(root, path):
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
def t_read(root, path, start=None, end=None):
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


def t_search(root, pattern, path=".", mode="auto", limit=40, glob=None):
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
def t_symbols(root, query="", path=None, limit=50):
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
def overview(root, maxrows=25):
    out = t_tree(root, ".", depth=1, maxrows=maxrows).split("\n")
    return "\n".join(out[:-1])
