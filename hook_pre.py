#!/usr/bin/env python3
"""ctxguard PreToolUse hook：在工具真正执行前，拒绝“把大块内容读进上下文”的调用。

拦截对象：Read（整份/大区间）、Grep（content 模式无上限）、Bash（cat/head/sed 读大文件、
无上限的递归 grep / git diff / find 等）。
拒绝时 exit 2 并把 stderr 反馈给模型：反馈里带目录和下一步，模型会改走 tree→outline→read。
任何内部异常一律放行（fail-open），避免 hook 自身故障卡死会话。
"""
import fnmatch
import json
import os
import re
import shlex
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lib  # noqa: E402
import tools  # noqa: E402


def deny(msg):
    sys.stderr.write(msg.rstrip() + "\n")
    sys.exit(2)


# ---------------------------------------------------------------- 会话累计预算
def _state_path(root, sid):
    return os.path.join(root, lib.INDEX_DIR, "session-%s.json" % re.sub(r"[^\w\-]", "_", sid or "default"))


def _load(root, sid):
    try:
        with open(_state_path(root, sid), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"served": 0}


def _save(root, sid, st):
    try:
        os.makedirs(os.path.join(root, lib.INDEX_DIR), exist_ok=True)
        with open(_state_path(root, sid), "w", encoding="utf-8") as f:
            json.dump(st, f)
    except Exception:
        pass


def charge(root, d, cfg, tok):
    """主线程累计读取预算；子 agent（带 agent_id）不计入，那里正是“读很多”的指定场所。"""
    budget = cfg.session_budget
    if budget <= 0 or d.get("agent_id"):
        return
    sid = d.get("session_id") or "default"
    st = _load(root, sid)
    if st.get("served", 0) + tok > budget:
        deny(f"[ctxguard] 拒绝：本会话主线程累计已读取约 {st.get('served', 0)}tok，预算 {budget}tok 用尽。"
             "需要继续通读材料时，改用子 agent（Agent 工具，prompt 里要求只返回结论和 路径:行号），"
             "或先 /compact、/clear。")
    st["served"] = st.get("served", 0) + tok
    _save(root, sid, st)


# ---------------------------------------------------------------- Read
def handle_read(root, d, cfg):
    ti = d.get("tool_input") or {}
    fp = ti.get("file_path") or ""
    if not fp or not os.path.isfile(fp):
        return
    ab, rel = tools.resolve(root, fp)
    if any(fnmatch.fnmatch(rel, g) for g in cfg.allow):
        return
    ext = os.path.splitext(fp)[1].lower()
    size = os.path.getsize(fp)
    if ext == ".pdf":
        if not ti.get("pages") and size > 2 * 1024 * 1024:
            deny(f"[ctxguard] 拒绝：{rel} 是 {size // 1024}KB 的 PDF，未指定 pages 会整份送入上下文。"
                 "请用 Read 的 pages 参数分页（如 \"1-5\"），或先转成文本再用 outline/read 导航。")
        return
    if ext in lib.BIN_EXTS:
        return
    with open(fp, "rb") as f:
        if b"\0" in f.read(8192):
            return
    if size <= lib.MAX_READ_TOK * 2:
        charge(root, d, cfg, size // 3)
        return
    v = tools.longline_view(root, ab)
    if v:
        deny(tools.big_message(root, ab, ""))
    lines = lib.read_lines(fp)
    s = max(1, int(ti.get("offset") or 1))
    n = int(ti.get("limit") or 2000)
    seg = lines[s - 1:s - 1 + n]
    tok = tools.lines_tokens(seg)
    if tok > lib.MAX_READ_TOK:
        deny(tools.big_message(root, ab, f"的请求区间 {s}-{s + len(seg) - 1}"))
    charge(root, d, cfg, tok)


# ---------------------------------------------------------------- Grep
def handle_grep(root, d, cfg):
    ti = d.get("tool_input") or {}
    if (ti.get("output_mode") or "files_with_matches") != "content":
        return
    cap = cfg.grep_content_max
    hl = ti.get("head_limit")
    if hl is None or int(hl) == 0 or int(hl) > cap:
        deny(f"[ctxguard] 拒绝：Grep 的 content 模式必须设置 head_limit ≤ {cap}。"
             "先用 output_mode=files_with_matches 或 count 看分布，缩小 path 后再用 content；"
             "或改用 ctxguard 的 search（文件→段→行 三级下钻）。")


# ---------------------------------------------------------------- Bash
SEP = {"|", "||", "&&", ";", "&", "|&"}
PREFIX = {"sudo", "env", "time", "nice", "command", "nohup", "timeout", "stdbuf"}
READERS = {"cat", "bat", "batcat", "less", "more", "nl", "tac", "strings", "xxd", "od", "hexdump"}
BOUNDERS = {"head", "tail", "wc"}
GREP_LIMITERS = set("lcLq")
DIFF_LIMITERS = {"--stat", "--shortstat", "--numstat", "--name-only", "--name-status", "--summary",
                 "--quiet", "-q", "--exit-code", "--check", "--dirstat", "--no-patch", "-s", "--oneline"}


def _segments(cmd):
    lex = shlex.shlex(cmd, posix=True, punctuation_chars=True)
    lex.whitespace_split = True
    segs, cur = [], []
    for t in lex:
        if t in SEP:
            segs.append([cur, t])
            cur = []
        else:
            cur.append(t)
    segs.append([cur, None])
    return segs


def _strip(words):
    w = list(words)
    while w:
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", w[0]):
            w.pop(0)
            continue
        b = os.path.basename(w[0])
        if b in PREFIX:
            w.pop(0)
            if b == "timeout" and w and not w[0].startswith("-"):
                w.pop(0)
            while w and w[0].startswith("-"):
                w.pop(0)
            continue
        break
    return w


def _redirected(words):
    for i, t in enumerate(words):
        if t in ("&>", "&>>"):
            return True
        if t in (">", ">>") and i + 1 < len(words) and words[i + 1] != "/dev/null" and \
                (i == 0 or words[i - 1] not in ("2",)):
            return True
    return False


def _bounded(segs, i):
    """输出是否被限制：重定向到文件，或管道末端接了 head/tail/wc。"""
    if _redirected(segs[i][0]):
        return True
    j = i
    while segs[j][1] in ("|", "|&") and j + 1 < len(segs):
        j += 1
        w = _strip(segs[j][0])
        if w and os.path.basename(w[0]) in BOUNDERS:
            return True
    return False


def _piped(segs, i):
    return segs[i][1] in ("|", "|&")


def _files(args, cwd):
    out = []
    for a in args:
        if a.startswith("-") and a != "-":
            continue
        p = a if os.path.isabs(a) else os.path.join(cwd, a)
        if os.path.isfile(p):
            out.append(p)
    return out


def _num(s, default):
    try:
        return int(s)
    except (TypeError, ValueError):
        return default


def _check_headtail(base, words, cwd, root):
    n, files, i = 10, [], 1
    while i < len(words):
        t = words[i]
        m = re.match(r"^-n(\+?\d+)$|^--lines=(\+?\d+)$|^-(\d+)$", t)
        if t in ("-n", "--lines") and i + 1 < len(words):
            n = 10 ** 9 if words[i + 1].startswith("+") else _num(words[i + 1], 10)
            i += 2
            continue
        if m:
            g = next(x for x in m.groups() if x)
            n = 10 ** 9 if g.startswith("+") else int(g)
        elif t in ("-c", "--bytes") and i + 1 < len(words):
            n = max(1, _num(words[i + 1].rstrip("kKmMbB"), 0) // 40)
            i += 2
            continue
        elif t in ("-f", "-F", "--follow"):
            return None
        elif not t.startswith("-"):
            files.append(t)
        i += 1
    for p in _files(files, cwd):
        lines = lib.read_lines(p)
        seg = lines[:n] if base == "head" else lines[-n:]
        if tools.lines_tokens(seg) > lib.MAX_READ_TOK:
            return tools.big_message(root, p, f"（{base} 取 {n} 行）")
    return None


def _check_sed(words, cwd, root):
    if any(w == "-i" or w.startswith("-i") or w == "--in-place" for w in words[1:]):
        return None
    args = [w for w in words[1:] if not w.startswith("-")]
    quiet = any(w == "-n" or (w.startswith("-") and not w.startswith("--") and "n" in w[1:]) for w in words[1:])
    if not args:
        return None
    script, files = args[0], _files(args[1:], cwd)
    for p in files:
        lines = lib.read_lines(p)
        m = re.match(r"^\s*(\d+)(?:,(\d+|\$))?p\s*$", script) if quiet else None
        if m:
            a = int(m.group(1))
            b = len(lines) if m.group(2) in (None, "$") and m.group(2) else (int(m.group(2)) if m.group(2) else a)
            seg = lines[a - 1:b]
        else:
            seg = lines
        if tools.lines_tokens(seg) > lib.MAX_READ_TOK:
            return tools.big_message(root, p, "（sed 取出的内容）")
    return None


def _git_sub(words):
    i = 1
    while i < len(words):
        if words[i] in ("-C", "-c", "--git-dir", "--work-tree", "--namespace") and i + 1 < len(words):
            i += 2
            continue
        if words[i].startswith("-"):
            i += 1
            continue
        return words[i], words[i + 1:]
    return None, []


def _short_has(words, letters):
    for t in words:
        if t.startswith("-") and not t.startswith("--") and any(ch in letters for ch in t[1:]):
            return True
    return False


GUIDE = ("改用 ctxguard 的 search（文件→段→行 三级下钻）定位，再用 read(path,start,end) 读小段；"
         "若必须用 shell，请把输出限制住（-l / -c / --stat / --name-only，或管道接 head -n 50）。")


def analyze_bash(cmd, cwd, root):
    try:
        segs = _segments(cmd)
    except ValueError:
        return None
    for i, (words, _) in enumerate(segs):
        w = _strip(words)
        if not w:
            continue
        base = os.path.basename(w[0])
        if base == "cd":
            if len(w) > 1 and not w[1].startswith("-"):
                cwd = w[1] if os.path.isabs(w[1]) else os.path.normpath(os.path.join(cwd, w[1]))
            continue
        bounded = _bounded(segs, i)
        if base in READERS and not bounded and not _piped(segs, i):
            for p in _files(w[1:], cwd):
                if tools.file_tokens(p) > lib.MAX_READ_TOK:
                    return tools.big_message(root, p, f"（{base} 整份输出）")
        elif base in ("head", "tail") and not _piped(segs, i) and not _redirected(w):
            r = _check_headtail(base, w, cwd, root)
            if r:
                return r
        elif base == "sed" and not bounded and not _piped(segs, i):
            r = _check_sed(w, cwd, root)
            if r:
                return r
        elif base in ("rg", "ag", "ack") or (base == "git" and _git_sub(w)[0] == "grep") or \
                (base == "grep" and (_short_has(w[1:], "rR") or "--recursive" in w)):
            lim = _short_has(w[1:], GREP_LIMITERS) or any(
                x in w for x in ("--count", "--files-with-matches", "--files-without-match", "--files", "--quiet"))
            if not lim and not bounded:
                return f"[ctxguard] 拒绝：{base} 全仓搜索的输出没有上限，会把大量匹配行灌进上下文。" + GUIDE
        elif base == "git":
            sub, rest = _git_sub(w)
            if sub in ("diff", "show") and not bounded and not (set(rest) & DIFF_LIMITERS):
                return (f"[ctxguard] 拒绝：git {sub} 未限制输出。先 `git {sub} --stat` 看改动分布，"
                        f"再 `git {sub} -- <单个文件> | head -150` 逐个查看。")
            if sub == "log" and not bounded:
                has_n = any(re.match(r"^-n\d*$|^-\d+$|^--max-count", x) for x in rest) or "-n" in rest
                patch = any(x in ("-p", "--patch", "-u", "--cc") for x in rest)
                if patch or not has_n:
                    return "[ctxguard] 拒绝：git log 未限制条数/含完整补丁。请加 -n 20 --oneline，要看补丁请只看单个提交的 --stat。"
        elif base == "tree" and not bounded and "-L" not in w:
            return "[ctxguard] 拒绝：tree 会输出整棵目录。请用 ctxguard 的 tree(path, depth)，或加 -L 2 并接 | head -80。"
        elif base == "ls" and not bounded and (_short_has(w[1:], "R") or "--recursive" in w):
            return "[ctxguard] 拒绝：ls -R 会输出整棵目录。请用 ctxguard 的 tree(path, depth)。"
        elif base == "find" and not bounded and "-maxdepth" not in w and \
                not any(x in w for x in ("-name", "-iname", "-path", "-regex", "-newer", "-mtime", "-size")):
            return "[ctxguard] 拒绝：find 未加过滤会列出所有文件。请用 ctxguard 的 tree(path, depth)，或加 -maxdepth / -name 并接 | head -80。"
    return None


def handle_bash(root, d, cfg):
    if not cfg.bash_guard:
        return
    cmd = (d.get("tool_input") or {}).get("command") or ""
    msg = analyze_bash(cmd, d.get("cwd") or root, root)
    if msg:
        deny(msg)


def main():
    try:
        d = json.load(sys.stdin)
    except Exception:
        return
    try:
        root = tools.find_root(d.get("cwd") or os.getcwd())
        cfg = tools.load_config(root)
        name = d.get("tool_name")
        if name == "Read":
            handle_read(root, d, cfg)
        elif name == "Grep":
            handle_grep(root, d, cfg)
        elif name == "Bash":
            handle_bash(root, d, cfg)
    except SystemExit:
        raise
    except Exception:  # noqa: BLE001  fail-open
        return


if __name__ == "__main__":
    main()
