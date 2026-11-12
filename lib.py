#!/usr/bin/env python3
"""ctxguard 公共库：语言无关的分块 / 索引 / token 估算。仅依赖标准库。

思路：不理解语法，只用通用启发式（声明关键字、缩进层级、标题、空行）把任何文本文件
切成“带标题和摘要的小段”，存进 SQLite。之后模型只能按 目录 -> 段 -> 行 下钻。
"""
import bisect
import contextlib
import io
import os
import re
import sqlite3
import subprocess
import sys
import time

INDEX_DIR = ".ctxindex"
MAX_CHUNK_LINES = int(os.environ.get("CTXGUARD_CHUNK_LINES", "120"))
MAX_CHUNK_CHARS = int(os.environ.get("CTXGUARD_CHUNK_CHARS", "6000"))
MIN_CHUNK_LINES = 15
LINE_CLIP = 1000
MAX_READ_TOK = int(os.environ.get("CTXGUARD_MAX_READ_TOK", "4000"))  # 单次读取上限（估算 tokens）
MAX_INDEX_BYTES = 20 * 1024 * 1024

# 声明提取后端（见 backends.py）。由 tools.load_config 设置；为空则只用内置启发式。
BACKENDS = []
BACKEND_OPTS = {"descend_functions": False, "max_depth": 6}
BACKEND_IGNORED = []        # 被安全策略忽略的项目级后端名（doctor 会显示）
CONFIG_LOADED: bool = False  # 未加载配置的进程（如只读 chunks_for）不做索引签名校验，避免互相清库
INDEX_VERSION = "3"

DOC_EXTS = {".md", ".markdown", ".mdx", ".rst", ".txt", ".adoc", ".asciidoc", ".org"}
IMG_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".ico", ".tiff", ".heic"}
BIN_EXTS = IMG_EXTS | {
    ".pdf", ".zip", ".gz", ".tar", ".tgz", ".bz2", ".xz", ".7z", ".rar", ".jar", ".class", ".so",
    ".dll", ".dylib", ".exe", ".bin", ".woff", ".woff2", ".ttf", ".otf", ".eot", ".mp3", ".mp4",
    ".mov", ".avi", ".wav", ".wasm", ".pyc", ".o", ".a", ".lib", ".psd", ".sqlite", ".db", ".onnx",
    ".pt", ".pth", ".npy", ".npz", ".parquet", ".xlsx", ".xls", ".docx", ".pptx",
}
NO_DECL_EXTS = {".json", ".jsonl", ".csv", ".tsv", ".lock", ".svg", ".xml", ".html", ".htm", ".map", ".log"}
SKIP_DIRS = {
    ".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv", "dist", "build", "target",
    ".next", INDEX_DIR, ".idea", ".gradle", ".tox", ".mypy_cache", ".pytest_cache", ".cache",
}


# ---------------------------------------------------------------- 标准输出
def stdout():
    """标准输出流。个别环境（pythonw、被重定向成 None）下 sys.stdout 为 None，退回空流避免崩溃。"""
    return sys.stdout if sys.stdout is not None else io.StringIO()


def set_stdout_encoding():
    """把标准输出切到 UTF-8；环境不支持 reconfigure 时静默跳过。"""
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if reconfigure is not None:
        reconfigure(encoding="utf-8")


def set_stdin_encoding():
    """把标准输入切到 UTF-8；环境不支持 reconfigure 时静默跳过。"""
    reconfigure = getattr(sys.stdin, "reconfigure", None)
    if reconfigure is not None:
        reconfigure(encoding="utf-8")


# ---------------------------------------------------------------- 估算
def est_tokens(text):
    """粗估 token：ASCII 约 3.6 字符/token，非 ASCII（中日韩）约 1 字符/token。"""
    if not text:
        return 0
    na = len(text) - len(text.encode("ascii", "ignore"))
    return int((len(text) - na) / 3.6 + na * 1.0) + 1


def _clip(s, n):
    s = " ".join(s.split())
    return s if len(s) <= n else s[: n - 1] + "…"


def indent_of(line):
    n = 0
    for ch in line:
        if ch == " ":
            n += 1
        elif ch == "\t":
            n += 4
        else:
            break
    return n


def split_lines(text):
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return [ln[:-1] if ln.endswith("\r") else ln for ln in lines]


def read_lines(path):
    with open(path, "rb") as f:
        data = f.read(MAX_INDEX_BYTES)
    return split_lines(data.decode("utf-8", "replace"))


# ---------------------------------------------------------------- 声明识别（跨语言启发式）
_MOD = (r"(?:(?:export|public|private|protected|static|async|abstract|final|pub(?:\([^)]*\))?|override|"
        r"default|extern|inline|unsafe|const|sealed|internal|open|suspend|virtual|partial|readonly|"
        r"declare|external|data|@[\w.]+(?:\([^)]*\))?)\s+)*")
_KW = (r"(?:defmodule|defmacrop?|defprotocol|defimpl|defstruct|defp|defn-?|def|fn|func|function|class|struct|"
       r"enum|interface|trait|impl|type|module|mod|namespace|object|record|protocol|extension|macro|proc|"
       r"sub|fun|template|contract|message|service|union|subroutine|procedure|package)")
DECL_KW = re.compile(r"^\s*" + _MOD + _KW + r"""(?:\s+[\w$<(\[*"'`]|<)""")
DECL_ARROW = re.compile(
    r"^\s*(?:export\s+)?(?:default\s+)?(?:const|let|var)\s+[\w$]+\s*(?::[^=]+)?=\s*"
    r"(?:async\s*)?(?:function\b|\([^)]*\)\s*(?::\s*[^=]+)?=>|[\w$]+\s*=>)")
_CTRL = (r"(?!\s*(?:if|else|for|while|switch|catch|return|do|try|elif|with|using|lock|foreach|when|match|"
         r"unless|until|await|throw|new|delete|sizeof|synchronized|assert|yield|print|echo)\b)")
DECL_METHOD = re.compile(
    r"^" + _CTRL + r"\s*(?:[\w$<>\[\],.?*&:~]+\s+)*[\w$~]+\s*\([^;{}]*\)\s*"
    r"(?:const|override|final|noexcept|throws\s+[\w., ]+|->\s*[\w:<>*&]+|:\s*[\w<>\[\]|. ]+)?\s*\{\s*$")
DECL_C_SIG = re.compile(r"^" + _CTRL + r"[A-Za-z_][\w\s\*&:<>,~\[\]]*\([^;{}]*\)\s*(?:const|noexcept|override)?\s*$")
DECL_SH = re.compile(r"^\s*(?:function\s+)?[\w:.-]+\s*\(\)\s*\{?\s*$")
DECL_LISP = re.compile(r"^\((?:def[\w-]*|define[\w-]*)\b")
DECL_SQL = re.compile(
    r"^\s*create\s+(?:or\s+replace\s+)?(?:temp\w*\s+|materialized\s+)?"
    r"(?:function|procedure|table|view|trigger|index|type|schema)\b", re.I)
DECL_SECT = re.compile(r"^\[[^\]\n]+\]\s*$")
EXT_DECL = {
    ".yaml": re.compile(r"^[\w.\-\"']+:\s*(?:#.*)?$"),
    ".yml": re.compile(r"^[\w.\-\"']+:\s*(?:#.*)?$"),
    ".toml": DECL_SECT, ".ini": DECL_SECT, ".cfg": DECL_SECT,
    ".css": re.compile(r"^[^\s}/][^{};]*\{\s*$"),
    ".scss": re.compile(r"^[^\s}/][^{};]*\{\s*$"),
    ".less": re.compile(r"^[^\s}/][^{};]*\{\s*$"),
}
LEAD = re.compile(
    r"^\s*(?:@\w|#\[|//|/\*|\*|--|;;|#(?!\s*(?:include|define|pragma|if|ifdef|ifndef|endif|else|elif|undef|import)\b))")
_COMMENT_MARK = re.compile(r"^(?:///?!?|/\*+|\*+/?|#+(?=\s|$)|--|;;+|\"\"\"|'''|\(\*)\s*")


def find_decls(lines, ext):
    """返回 [(行号0基, 缩进, 声明文本)]。"""
    if ext in NO_DECL_EXTS:
        return []
    extra = EXT_DECL.get(ext)
    out = []
    n = len(lines)
    for i, ln in enumerate(lines):
        if not ln or ln.isspace() or len(ln) > 400:
            continue
        hit = False
        if extra is not None:
            hit = bool(extra.match(ln))
        else:
            hit = bool(DECL_KW.match(ln) or DECL_ARROW.match(ln) or DECL_METHOD.match(ln) or DECL_SH.match(ln)
                       or DECL_LISP.match(ln) or DECL_SQL.match(ln) or DECL_SECT.match(ln))
            if not hit and ln[0] not in " \t{}#/*" and DECL_C_SIG.match(ln):
                j = i + 1
                while j < n and not lines[j].strip():
                    j += 1
                hit = j < n and lines[j].lstrip().startswith("{")
        if hit:
            out.append((i, indent_of(ln), ln.strip()[:120]))
    return out


# ---------------------------------------------------------------- 分块
def _chars(lines, a, b):
    return sum(min(len(x), LINE_CLIP) + 1 for x in lines[a:b])


def _fits(lines, a, b):
    return (b - a) <= MAX_CHUNK_LINES and _chars(lines, a, b) <= MAX_CHUNK_CHARS


def _lead(lines, floor, i):
    j = i
    while j > floor and LEAD.match(lines[j - 1]):
        j -= 1
    return j


def _first_line(lines, a, b):
    for k in range(a, b):
        if lines[k].strip():
            return _clip(lines[k].strip(), 80)
    return "(空)"


def _split_region(lines, a, b, decls, idxs, title, out):
    """递归：区域放得下就整段输出；否则按最浅缩进的声明切开，再对每段递归。"""
    if a >= b:
        return
    if _fits(lines, a, b) or not decls:
        out.append((a, b, title))
        return
    mi = min(d[1] for d in decls)
    tops = [d for d in decls if d[1] == mi]
    starts = []
    for d in tops:
        floor = starts[-1][1][0] + 1 if starts else a
        starts.append((_lead(lines, floor, d[0]), d))
    if starts[0][0] > a:
        out.append((a, starts[0][0], (title + " (头部)") if title else "(文件头)"))
    for k, (st, d) in enumerate(starts):
        nxt = starts[k + 1][0] if k + 1 < len(starts) else b
        end, gap = nxt, None
        de = d[3] if len(d) > 3 else None       # 后端提供了声明结束行：精确切开，间隙里的顶层代码单独成段
        if de is not None:
            cut = min(max(de + 1, st + 1), nxt)
            if cut < nxt and any(lines[x].strip() for x in range(cut, nxt)):
                end, gap = cut, (cut, nxt)
        lo = bisect.bisect_left(idxs, st)
        hi = bisect.bisect_left(idxs, end)
        subs = [x for x in decls[lo:hi] if x[1] > mi and st <= x[0] < end]
        sub_idxs = [x[0] for x in subs]
        ptitle = (title + " > " + d[2]) if title else d[2]
        _split_region(lines, st, end, subs, sub_idxs, ptitle, out)
        if gap:
            out.append((gap[0], gap[1], (title + " > " if title else "") + "(其他代码)"))


def _windows(lines, a, b):
    parts = []
    st = a
    while st < b:
        en, chars = st, 0
        while en < b and en - st < MAX_CHUNK_LINES:
            c = min(len(lines[en]), LINE_CLIP) + 1
            if chars + c > MAX_CHUNK_CHARS and en > st:
                break
            chars += c
            en += 1
        if en < b:
            lo = st + int((en - st) * 0.6)
            for j in range(en - 1, lo, -1):
                if not lines[j].strip():
                    en = j + 1
                    break
        parts.append((st, en))
        st = en
    return parts


def _merge(exp, lines):
    res = []
    for p in exp:
        if res:
            q = res[-1]
            if ((q["b"] - q["a"]) < MIN_CHUNK_LINES and (p["b"] - q["a"]) <= MAX_CHUNK_LINES * 0.9
                    and _chars(lines, q["a"], p["b"]) <= MAX_CHUNK_CHARS * 0.9):
                q["b"] = p["b"]
                q["n"] += 1
                q["last"] = p["title"]
                continue
        res.append({"a": p["a"], "b": p["b"], "title": p["title"], "n": 1, "last": p["title"]})
    return res


def summarize_code(lines, a, b):
    for k in range(a, min(b, a + 6)):
        t = lines[k].strip()
        m = _COMMENT_MARK.match(t)
        if m:
            rest = re.sub(r"(\*/|\"\"\"|''')\s*$", "", t[m.end():]).strip()
            if rest:
                return rest
    return ""


_UNDERLINE = re.compile(r"^[=\-~^\"'`#*+:._]{3,}$")


def summarize_doc(lines, a, b):
    for k in range(a, min(b, a + 12)):
        t = lines[k].strip()
        if not t or re.match(r"^(#{1,6}\s|```|~~~|\||[=\-~^]{3,}$)", t):
            continue
        if re.match(r"^(=+|\*+)\s+\S", t):
            continue
        if k + 1 < len(lines) and _UNDERLINE.match(lines[k + 1].strip()):
            continue
        t = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", t)
        t = re.sub(r"^[\s>*+\-#=`|_~]+", "", t).strip()
        if t:
            return t
    return ""


def _finalize(lines, pieces, is_doc):
    exp = []
    for a, b, title in pieces:
        if _fits(lines, a, b):
            exp.append({"a": a, "b": b, "title": title or _first_line(lines, a, b)})
        else:
            parts = _windows(lines, a, b)
            for k, (x, y) in enumerate(parts, 1):
                t = title or _first_line(lines, x, y)
                exp.append({"a": x, "b": y, "title": f"{t} [{k}/{len(parts)}]" if len(parts) > 1 else t})
    res = []
    for m in _merge(exp, lines):
        a, b = m["a"], m["b"]
        t = m["title"] if m["n"] == 1 else f'{m["title"]} … {m["last"]} (+{m["n"] - 1})'
        summ = summarize_doc(lines, a, b) if is_doc else summarize_code(lines, a, b)
        res.append({"s": a + 1, "e": b, "title": _clip(t, 160), "summary": _clip(summ, 110),
                    "tok": est_tokens("\n".join(lines[a:b]))})
    return res


def _md_headings(lines):
    hs, fence = [], None
    for i, ln in enumerate(lines):
        s = ln.lstrip()
        if s.startswith("```") or s.startswith("~~~"):
            if fence is None:
                fence = s[:3]
            elif fence == s[:3]:
                fence = None
            continue
        if fence:
            continue
        m = re.match(r"^ {0,3}(#{1,6})\s+(.+?)\s*#*\s*$", ln)
        if m:
            hs.append((i, len(m.group(1)), m.group(2)))
    return hs


def _re_headings(lines, pat):
    rx = re.compile(pat)
    hs = []
    for i, ln in enumerate(lines):
        m = rx.match(ln)
        if m:
            hs.append((i, len(m.group(1)), m.group(2)))
    return hs


def _rst_headings(lines):
    hs, levels = [], {}
    sym = set("=-~^\"'`#*+:._")
    for i in range(len(lines) - 1):
        t, u = lines[i].strip(), lines[i + 1].strip()
        if t and len(u) >= 3 and len(set(u)) == 1 and u[0] in sym and len(u) >= len(t) - 1 and not set(t) <= sym:
            lvl = levels.setdefault(u[0], len(levels) + 1)
            hs.append((i, lvl, t))
    return hs


def doc_pieces(lines, ext):
    if ext == ".rst":
        hs = _rst_headings(lines)
    elif ext in (".adoc", ".asciidoc"):
        hs = _re_headings(lines, r"^(={1,6})\s+(\S.*?)\s*=*\s*$")
    elif ext == ".org":
        hs = _re_headings(lines, r"^(\*+)\s+(\S.*)$")
    else:
        hs = _md_headings(lines)
    if not hs:
        return [(0, len(lines), "")], []
    pieces, syms, stack = [], [], []
    if hs[0][0] > 0 and any(x.strip() for x in lines[: hs[0][0]]):
        pieces.append((0, hs[0][0], "(开头)"))
    for k, (i, lvl, text) in enumerate(hs):
        while stack and stack[-1][0] >= lvl:
            stack.pop()
        stack.append((lvl, text))
        crumb = " > ".join(t for _, t in stack)
        if len(crumb) > 160:
            crumb = "…" + crumb[-159:]
        end = hs[k + 1][0] if k + 1 < len(hs) else len(lines)
        pieces.append((i, end, crumb))
        syms.append((i + 1, crumb))
    return pieces, syms


def chunk_text(text, ext):
    """返回 (lines, chunks, symbols)。chunks: [{s,e,title,summary,tok}]，行号 1 基、闭区间。"""
    lines = split_lines(text)
    if not lines:
        return lines, [], []
    if ext in DOC_EXTS:
        pieces, syms = doc_pieces(lines, ext)
        chunks = _finalize(lines, pieces, True)
    else:
        decls = None
        if BACKENDS:
            import backends
            decls = backends.get_decls("\n".join(lines), ext, BACKENDS, BACKEND_OPTS, len(lines))
        if decls is None:
            decls = find_decls(lines, ext)
        pieces = []
        _split_region(lines, 0, len(lines), decls, [d[0] for d in decls], "", pieces)
        chunks = _finalize(lines, pieces, False)
        syms = [(d[0] + 1, d[2]) for d in decls]
    return lines, chunks, syms[:3000]


# ---------------------------------------------------------------- 索引库
SCHEMA = """
CREATE TABLE IF NOT EXISTS files(path TEXT PRIMARY KEY, size INTEGER, mtime REAL, kind TEXT,
                                 lines INTEGER, tok INTEGER, nchunks INTEGER);
CREATE TABLE IF NOT EXISTS chunks(path TEXT, idx INTEGER, s INTEGER, e INTEGER, tok INTEGER,
                                  title TEXT, summary TEXT, PRIMARY KEY(path, idx));
CREATE TABLE IF NOT EXISTS symbols(path TEXT, line INTEGER, name TEXT);
CREATE INDEX IF NOT EXISTS sym_name ON symbols(name);
CREATE INDEX IF NOT EXISTS sym_path ON symbols(path);
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
"""


def open_db(root):
    d = os.path.join(root, INDEX_DIR)
    os.makedirs(d, exist_ok=True)
    gi = os.path.join(d, ".gitignore")
    if not os.path.exists(gi):
        with open(gi, "w") as f:
            f.write("*\n")
    con = sqlite3.connect(os.path.join(d, "index.db"), timeout=30)
    with contextlib.suppress(sqlite3.Error):
        con.execute("PRAGMA journal_mode=WAL")
    con.executescript(SCHEMA)
    if CONFIG_LOADED:
        try:
            sig = index_signature()
            row = con.execute("SELECT v FROM meta WHERE k='sig'").fetchone()
            if not row or row[0] != sig:      # 后端/版本/分块参数变了：旧索引作废，下次自动重建
                for t in ("files", "chunks", "symbols"):
                    con.execute("DELETE FROM " + t)
                con.execute("REPLACE INTO meta VALUES('sig', ?)", (sig,))
                con.commit()
        except Exception:
            pass
    return con


def index_signature():
    import backends
    return "v%s|%d|%d|%s" % (INDEX_VERSION, MAX_CHUNK_LINES, MAX_CHUNK_CHARS,
                             backends.signature(BACKENDS, BACKEND_OPTS))


def list_files(root):
    paths = None
    try:
        r = subprocess.run(["git", "-C", root, "ls-files", "-co", "--exclude-standard", "-z"],
                           capture_output=True, timeout=60)
        if r.returncode == 0:
            paths = [p for p in r.stdout.decode("utf-8", "replace").split("\0") if p]
    except Exception:
        pass
    if paths is None:
        paths = []
        for dp, dns, fns in os.walk(root):
            dns[:] = [d for d in dns if d not in SKIP_DIRS]
            for fn in fns:
                paths.append(os.path.relpath(os.path.join(dp, fn), root).replace(os.sep, "/"))
    out = []
    for p in sorted(set(paths)):
        if any(x in SKIP_DIRS for x in p.split("/")[:-1]):
            continue
        fp = os.path.join(root, p)
        if os.path.isfile(fp) and not os.path.islink(fp):
            out.append(p)
    return out


def index_one(con, root, rel):
    p = os.path.join(root, rel)
    try:
        st = os.stat(p)
    except OSError:
        for t in ("files", "chunks", "symbols"):
            con.execute(f"DELETE FROM {t} WHERE path=?", (rel,))
        return None
    ext = os.path.splitext(rel)[1].lower()
    kind, nlines, tok, chunks, syms = ("doc" if ext in DOC_EXTS else "code"), 0, 0, [], []
    if ext in BIN_EXTS:
        kind = "binary"
    elif st.st_size > MAX_INDEX_BYTES:
        kind, tok = "huge", int(st.st_size / 3.5)
    else:
        with open(p, "rb") as f:
            data = f.read()
        if b"\0" in data[:8192]:
            kind = "binary"
        else:
            text = data.decode("utf-8", "replace")
            lines, chunks, syms = chunk_text(text, ext)
            nlines, tok = len(lines), est_tokens(text)
    con.execute("DELETE FROM chunks WHERE path=?", (rel,))
    con.execute("DELETE FROM symbols WHERE path=?", (rel,))
    con.execute("REPLACE INTO files VALUES(?,?,?,?,?,?,?)",
                (rel, st.st_size, st.st_mtime, kind, nlines, tok, len(chunks)))
    con.executemany("INSERT INTO chunks VALUES(?,?,?,?,?,?,?)",
                    [(rel, i, c["s"], c["e"], c["tok"], c["title"], c["summary"]) for i, c in enumerate(chunks, 1)])
    con.executemany("INSERT INTO symbols VALUES(?,?,?)", [(rel, ln, nm) for ln, nm in syms])
    return True


def build(root, con=None, time_limit=None, force=False):
    own = con is None
    con = con or open_db(root)
    t0 = time.time()
    paths = list_files(root)
    have = {r[0]: (r[1], r[2]) for r in con.execute("SELECT path,size,mtime FROM files")}
    done = changed = 0
    for rel in paths:
        if time_limit and time.time() - t0 > time_limit:
            break
        try:
            st = os.stat(os.path.join(root, rel))
        except OSError:
            done += 1
            continue
        h = have.get(rel)
        if not force and h and h[0] == st.st_size and abs(h[1] - st.st_mtime) < 1e-6:
            done += 1
            continue
        index_one(con, root, rel)
        changed += 1
        done += 1
        if changed % 200 == 0:
            con.commit()
    if done == len(paths):
        for gone in set(have) - set(paths):
            index_one(con, root, gone)
    con.commit()
    if own:
        con.close()
    return {"total": len(paths), "done": done, "changed": changed, "partial": done < len(paths)}


def ensure_file(con, root, rel):
    """保证单个文件的索引是新的，返回 files 行 (path,size,mtime,kind,lines,tok,nchunks) 或 None。"""
    try:
        st = os.stat(os.path.join(root, rel))
    except OSError:
        return None
    row = con.execute("SELECT * FROM files WHERE path=?", (rel,)).fetchone()
    if not row or row[1] != st.st_size or abs(row[2] - st.st_mtime) > 1e-6:
        index_one(con, root, rel)
        row = con.execute("SELECT * FROM files WHERE path=?", (rel,)).fetchone()
    return row


def chunks_for(root, abspath):
    """返回 [(s,e,tok,title,summary)]；优先用索引，失败则现算（文件在项目外也能用）。"""
    try:
        rel = os.path.relpath(abspath, root).replace(os.sep, "/")
        if rel.startswith(".."):
            raise ValueError("outside")
        con = open_db(root)
        ensure_file(con, root, rel)
        con.commit()
        rows = con.execute("SELECT s,e,tok,title,summary FROM chunks WHERE path=? ORDER BY idx", (rel,)).fetchall()
        con.close()
        if rows:
            return rows
    except Exception:
        pass
    try:
        with open(abspath, "rb") as f:
            data = f.read(MAX_INDEX_BYTES)
        _, chunks, _ = chunk_text(data.decode("utf-8", "replace"), os.path.splitext(abspath)[1].lower())
        return [(c["s"], c["e"], c["tok"], c["title"], c["summary"]) for c in chunks]
    except Exception:
        return []


def outline_text(rows, maxrows=60):
    out = []
    for s, e, tok, title, summ in rows[:maxrows]:
        t = title if len(title) <= 90 else title[:89] + "…"
        extra = f" — {summ[:60]}" if summ and summ[:20] not in t else ""
        out.append(f"  {s}-{e} | ~{tok}tok | {t}{extra}")
    if len(rows) > maxrows:
        out.append(f"  …… 另有 {len(rows) - maxrows} 段未列出（共 {len(rows)} 段）：先按标题判断需要哪部分，"
                   f"或用 grep/symbols 定位")
    return "\n".join(out)


def big_file_message(root, path, what):
    """给被拒绝的大文件生成“目录 + 下一步怎么做”的反馈。"""
    rel = os.path.relpath(path, root) if path.startswith(root) else path
    rows = chunks_for(root, path)
    total = sum(r[2] for r in rows) if rows else int(os.path.getsize(path) / 3.2)
    head = (f"[ctxguard] 拒绝：{rel}（约 {total} tokens，{len(rows)} 段）{what}，"
            f"超过单次上限 {MAX_READ_TOK} tokens。不要整份读取。")
    if not rows:
        return head + "\n文件过大且无法建立目录：请用 grep 定位后，用 sed -n 'a,bp' / Read(offset,limit) 读小范围。"
    return (head + "\n下面是该文件的目录（起止行 | tokens | 标题 — 摘要）。只挑需要的段，"
            "用 Read(offset=起始行, limit=行数) 或 ctxguard 的 read 读那一段：\n" + outline_text(rows))
