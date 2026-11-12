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
BUDGET = CFG.session_budget
served = 0

# 工具清单与参数 schema 都来自 tools.TOOLS 注册表，不再另写一份
TOOLS = [{"name": t.name, "description": t.summary, "inputSchema": tools.tool_schema(t)}
         for t in tools.TOOLS]


def call(name, a):
    global served
    tool = tools.BY_NAME.get(name)
    if tool is None:
        raise ValueError(f"未知工具：{name}")
    text = tools.call_tool(tool, ROOT, a)
    if tool.name == "read" and not text.startswith(("REFUSED", "ERROR")):
        tok = lib.est_tokens(text)
        if BUDGET > 0 and served + tok > BUDGET:
            return (f"REFUSED: 本会话已通过 ctxguard 读取约 {served}tok，预算 {BUDGET}tok 用尽。"
                    "请把需要通读的材料交给子 agent（只让它返回结论和 路径:行号），或 /compact、/clear 后继续。")
        served += tok
    return text


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
