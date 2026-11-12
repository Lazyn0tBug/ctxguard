#!/usr/bin/env python3
"""ctxguard 代理：包在任意第三方 MCP 服务外面（stdio），把“巨大工具返回”转存成文件，只把目录还给模型。

用法（.mcp.json 里把原命令前面加上代理）：
  "command": "python3",
  "args": [".claude/ctxguard/proxy.py", "--", "npx", "some-mcp-server", "--flag"]

行为：转发所有消息；对 tools/call 的响应，若文本内容估算超过阈值（CTXGUARD_PROXY_TOK，默认 6000 tokens），
写入 .ctxindex/stash/，返回“已转存 + 目录 + 如何分段读”，原文不进入上下文，也就不会有“逐块读完”的诱导。
"""
import contextlib
import json
import os
import re
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lib  # noqa: E402
import tools  # noqa: E402

THRESH = int(os.environ.get("CTXGUARD_PROXY_TOK", "6000"))
ROOT = tools.find_root()
tools.load_config(ROOT)
STASH = os.path.join(ROOT, lib.INDEX_DIR, "stash")


def _cleanup(days=7):
    try:
        for fn in os.listdir(STASH):
            p = os.path.join(STASH, fn)
            if time.time() - os.path.getmtime(p) > days * 86400:
                os.remove(p)
    except OSError:
        pass


def stash(tool, result):
    content = result.get("content")
    if not isinstance(content, list):
        return result
    texts = [c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text"]
    total = "\n".join(texts)
    tok = lib.est_tokens(total)
    if tok <= THRESH:
        return result
    body = total
    with contextlib.suppress(Exception):
        body = json.dumps(json.loads(total), ensure_ascii=False, indent=1)
    os.makedirs(STASH, exist_ok=True)
    gi = os.path.join(ROOT, lib.INDEX_DIR, ".gitignore")
    if not os.path.exists(gi):
        with open(gi, "w") as f:
            f.write("*\n")
    name = "%s-%d.txt" % (re.sub(r"[^\w.\-]", "_", str(tool))[:40], int(time.time() * 1000))
    p = os.path.join(STASH, name)
    with open(p, "w", encoding="utf-8") as f:
        f.write(body)
    rel = os.path.relpath(p, ROOT).replace(os.sep, "/")
    rows = lib.chunks_for(ROOT, p)
    nlines = rows[-1].e if rows else body.count("\n") + 1
    msg = (f"[ctxguard] 工具 {tool} 返回内容约 {tok} tokens，已转存为 {rel}（{nlines} 行），未放入上下文。"
           "不要整份读取，也不要“逐块读完”：先看下面的目录，只读与任务相关的段"
           "（read(path,start,end) 或 Read(offset,limit)），或 search(pattern, path) 定位。\n"
           "目录（起止行 | tokens | 标题）：\n" + lib.outline_text(rows, 25))
    new = {k: v for k, v in result.items() if k not in ("content", "structuredContent")}
    new["content"] = [{"type": "text", "text": msg}] + [c for c in content if not (isinstance(c, dict) and c.get("type") == "text")]
    return new


def main():
    if "--" not in sys.argv:
        sys.stderr.write("usage: proxy.py -- <command> [args...]\n")
        sys.exit(2)
    cmd = sys.argv[sys.argv.index("--") + 1:]
    _cleanup()
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    proc_in, proc_out = proc.stdin, proc.stdout
    if proc_in is None or proc_out is None:   # 管道必然创建成功，除非被外部改写
        sys.stderr.write("proxy: 无法创建子进程管道\n")
        sys.exit(2)
    pending = {}

    def pump_in():
        try:
            for line in sys.stdin.buffer:
                try:
                    m = json.loads(line)
                    if m.get("method") == "tools/call" and "id" in m:
                        pending[m["id"]] = (m.get("params") or {}).get("name", "tool")
                except Exception:
                    pass
                proc_in.write(line)
                proc_in.flush()
        except (BrokenPipeError, OSError):
            pass
        finally:
            with contextlib.suppress(OSError):
                proc_in.close()

    threading.Thread(target=pump_in, daemon=True).start()
    out = sys.stdout.buffer
    for line in proc_out:
        try:
            m = json.loads(line)
            if isinstance(m, dict) and m.get("id") in pending and isinstance(m.get("result"), dict):
                m["result"] = stash(pending.pop(m["id"]), m["result"])
                line = (json.dumps(m, ensure_ascii=False) + "\n").encode("utf-8")
            elif isinstance(m, dict) and m.get("id") in pending:
                pending.pop(m["id"], None)
        except Exception:
            pass
        out.write(line)
        out.flush()
    sys.exit(proc.wait())


if __name__ == "__main__":
    main()
