#!/usr/bin/env python3
"""ctxguard MCP 服务（stdio，零依赖）：只提供“导航式读取”能力。

工具：tree / outline / read / search / symbols
设计要点：没有“读全文”的入口；read 必须给出行区间，且单次、累计都有上限，超限直接拒绝并返回目录。
"""
import json
import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lib  # noqa: E402
import tools  # noqa: E402

ROOT = tools.find_root()
CFG = tools.load_config(ROOT)
BUDGET = int(CFG.get("session_budget") or 0)
served = 0

TOOLS = [
    {"name": "tree",
     "description": "列出目录结构：每项带文件数/估算tokens/段数，不含正文。大目录自动折叠层级。读取任何文件之前先用它建立全局认识。",
     "inputSchema": {"type": "object", "properties": {
         "path": {"type": "string", "description": "目录路径，默认项目根"},
         "depth": {"type": "integer", "description": "展开层级，默认2"}}}},
    {"name": "outline",
     "description": "文件目录：按函数/类/标题切成段，每段给出起止行、tokens、标题和一句摘要。读文件前先看它，再决定读哪一段。",
     "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}},
    {"name": "read",
     "description": "按行区间读取文件的一小段（带行号）。单次有上限，超限会被拒绝并给出目录。小文件可省略区间。",
     "inputSchema": {"type": "object", "properties": {
         "path": {"type": "string"},
         "start": {"type": "integer", "description": "起始行(1基)"},
         "end": {"type": "integer", "description": "结束行(含)，省略则读200行"}}, "required": ["path"]}},
    {"name": "search",
     "description": "正则搜索，三级下钻：默认先返回 文件→命中的段（带次数），范围缩小后自动给出具体行。比全仓 grep 安全。",
     "inputSchema": {"type": "object", "properties": {
         "pattern": {"type": "string"},
         "path": {"type": "string", "description": "文件或目录，默认项目根"},
         "mode": {"type": "string", "enum": ["auto", "files", "lines"]},
         "glob": {"type": "string", "description": "文件名过滤，如 *.py"},
         "limit": {"type": "integer", "description": "lines 模式最多返回的行数(默认40，上限100)"}},
         "required": ["pattern"]}},
    {"name": "symbols",
     "description": "按名称子串查找函数/类/标题等声明，返回 路径:行号 | 声明。传文件路径且不传 query 则列出该文件全部声明。",
     "inputSchema": {"type": "object", "properties": {
         "query": {"type": "string"}, "path": {"type": "string"}, "limit": {"type": "integer"}}}},
]


def call(name, a):
    global served
    if name == "tree":
        return tools.t_tree(ROOT, a.get("path", "."), a.get("depth", 2))
    if name == "outline":
        return tools.t_outline(ROOT, a["path"])
    if name == "read":
        text = tools.t_read(ROOT, a["path"], a.get("start"), a.get("end"))
        if text.startswith(("REFUSED", "ERROR")):
            return text
        tok = lib.est_tokens(text)
        if BUDGET > 0 and served + tok > BUDGET:
            return (f"REFUSED: 本会话已通过 ctxguard 读取约 {served}tok，预算 {BUDGET}tok 用尽。"
                    "请把需要通读的材料交给子 agent（只让它返回结论和 路径:行号），或 /compact、/clear 后继续。")
        served += tok
        return text
    if name == "search":
        return tools.t_search(ROOT, a["pattern"], a.get("path", "."), a.get("mode", "auto"),
                              a.get("limit", 40), a.get("glob"))
    if name == "symbols":
        return tools.t_symbols(ROOT, a.get("query", ""), a.get("path"), a.get("limit", 50))
    raise ValueError(f"未知工具：{name}")


def send(obj):
    out = lib.stdout()
    out.write(json.dumps(obj, ensure_ascii=False) + "\n")
    out.flush()


def handle(msg):
    mid, method, params = msg.get("id"), msg.get("method"), msg.get("params") or {}
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": params.get("protocolVersion", "2024-11-05"),
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "ctxguard", "version": "1.0.0"}}})
    elif method == "ping":
        send({"jsonrpc": "2.0", "id": mid, "result": {}})
    elif method == "tools/list":
        send({"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}})
    elif method == "tools/call":
        try:
            text = call(params.get("name"), params.get("arguments") or {})
            err = text.startswith(("ERROR", "REFUSED"))
        except Exception as ex:  # noqa: BLE001
            text, err = f"ERROR: {type(ex).__name__}: {ex}", True
        send({"jsonrpc": "2.0", "id": mid, "result": {"content": [{"type": "text", "text": text}], "isError": err}})
    elif mid is not None:
        send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"method not found: {method}"}})


def main():
    lib.set_stdout_encoding()
    lib.set_stdin_encoding()
    threading.Thread(target=lambda: lib.build(ROOT, time_limit=60), daemon=True).start()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            handle(json.loads(line))
        except json.JSONDecodeError:
            continue


if __name__ == "__main__":
    main()
