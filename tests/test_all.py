#!/usr/bin/env python3
"""ctxguard 端到端测试：索引、hook、MCP 服务、代理。用法：test_all.py <ctxguard目录> <样例仓库>"""
import json
import os
import subprocess
import sys
import tempfile

SRC, REPO = sys.argv[1], sys.argv[2]
PY = sys.executable
env = dict(os.environ, CTXGUARD_ROOT=REPO)
results = []


def check(name, cond, detail=""):
    results.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name + (("  | " + detail[:300].replace("\n", " ⏎ ")) if (detail and not cond) else ""))


def hook(tool, ti, extra=None, script="hook_pre.py"):
    d = {"tool_name": tool, "tool_input": ti, "cwd": REPO, "session_id": "t-main"}
    d.update(extra or {})
    p = subprocess.run([PY, os.path.join(SRC, script)], input=json.dumps(d), capture_output=True, text=True, env=env)
    return p.returncode, p.stderr


def bash(cmd):
    return hook("Bash", {"command": cmd})


def pipes(p):
    """取出子进程的非空 stdin/stdout（管道模式下必定存在）。"""
    si, so = p.stdin, p.stdout
    assert si is not None and so is not None
    return si, so


def repo_path(p):
    return os.path.join(REPO, p)

# ---------------- Read
c, e = hook("Read", {"file_path": repo_path("svc/orders.py")})
check("Read 大文件整份 → 拒绝并给目录", c == 2 and "Handler0" in e and "outline" in e or (c == 2 and "起始行" in e or "Handler0" in e), e)
c, e = hook("Read", {"file_path": repo_path("svc/orders.py"), "offset": 26, "limit": 18})
check("Read 一小段 → 放行", c == 0, e)
c, e = hook("Read", {"file_path": repo_path("svc/orders.py"), "offset": 1, "limit": 2000})
check("Read 超大区间 → 拒绝", c == 2, e)
c, e = hook("Read", {"file_path": repo_path("README.md")})
check("Read 小文件 → 放行", c == 0, e)
c, e = hook("Read", {"file_path": repo_path("data/dump.json")})
check("Read 压缩 JSON → 拒绝并指向视图", c == 2 and "视图" in e, e)
views = os.path.join(REPO, ".ctxindex", "views")
vf = [f for f in os.listdir(views)] if os.path.isdir(views) else []
check("超长行视图已生成", len(vf) == 1, str(vf))
if vf:
    c, e = hook("Read", {"file_path": os.path.join(views, vf[0]), "offset": 1, "limit": 40})
    check("Read 视图分段 → 放行", c == 0, e)
c, e = hook("Read", {"file_path": repo_path("assets/logo.png")})
check("Read 二进制/图片 → 放行", c == 0, e)
c, e = hook("Read", {"file_path": repo_path("node_modules/x/big.js")})
check("Read 被忽略目录里的大文件 → 同样拒绝", c == 2, e)
c, e = hook("Read", {"file_path": repo_path("docs/plain.txt")})
check("Read 无结构大文本 → 拒绝并给分段目录", c == 2 and "1-120" in e, e)

# ---------------- Grep
check("Grep content 无 head_limit → 拒绝", hook("Grep", {"pattern": "x", "output_mode": "content"})[0] == 2)
check("Grep content head_limit=30 → 放行", hook("Grep", {"pattern": "x", "output_mode": "content", "head_limit": 30})[0] == 0)
check("Grep content head_limit=250 → 拒绝", hook("Grep", {"pattern": "x", "output_mode": "content", "head_limit": 250})[0] == 2)
check("Grep 默认 files_with_matches → 放行", hook("Grep", {"pattern": "x"})[0] == 0)

# ---------------- Bash
cases = [
    ("cat svc/orders.py", 2), ("cat svc/orders.py | head -20", 0), ("cat README.md", 0),
    ("cat svc/orders.py | grep foo", 0), ("cat svc/orders.py > /tmp/x.out", 0),
    ("head -n 50 svc/orders.py", 0), ("head -n 5000 svc/orders.py", 2), ("tail -n 20 svc/orders.py", 0),
    ("tail -n +2 svc/orders.py", 2),
    ("sed -n '1,40p' svc/orders.py", 0), ("sed -n '1,900p' svc/orders.py", 2), ("sed 's/a/b/' svc/orders.py", 2),
    ("cd svc && cat orders.py", 2), ("cd svc && head -n 30 orders.py", 0),
    ("git grep -n foo", 2), ("git grep -l foo", 0), ("git grep -c foo", 0), ("grep -rn foo .", 2),
    ("rg foo | head -20", 0), ("rg foo", 2), ("grep -n foo svc/orders.py", 0), ("grep -rl foo .", 0),
    ("git diff", 2), ("git diff --stat", 0), ("git diff -- svc/orders.py | head -100", 0), ("git show HEAD", 2),
    ("git log", 2), ("git log -n 5 --oneline", 0), ("git log -p -n 3", 2), ("git status", 0),
    ("find .", 2), ("find . -name '*.py'", 0), ("find . -maxdepth 2", 0), ("ls -R", 2), ("ls -la", 0), ("tree", 2),
    ("cat > x.txt <<'EOF'\nhello\nEOF", 0), ("echo hi && python3 -c 'print(1)'", 0), ("ls 2>/dev/null; cat README.md", 0),
    ("sudo cat svc/orders.py", 2), ("env FOO=1 cat svc/orders.py", 2), ("cat 'unterminated", 0),
]
for cmd, want in cases:
    c, e = bash(cmd)
    check(f"Bash {cmd.splitlines()[0]!r} → {'拒绝' if want == 2 else '放行'}", c == want, f"code={c} {e}")

# ---------------- 累计预算
cfgp = os.path.join(REPO, ".ctxguard.json")
with open(cfgp, "w") as f:
    json.dump({"session_budget": 500}, f)
for f in os.listdir(os.path.join(REPO, ".ctxindex")):
    if f.startswith("session-"):
        os.remove(os.path.join(REPO, ".ctxindex", f))
codes = [hook("Read", {"file_path": repo_path("svc/orders.py"), "offset": 1 + 18 * k, "limit": 18}, {"session_id": "bud"})[0] for k in range(5)]
check("累计预算：前几次放行，之后拒绝", codes[0] == 0 and codes[-1] == 2, str(codes))
check("累计预算：子 agent 不受限", hook("Read", {"file_path": repo_path("svc/orders.py"), "offset": 1, "limit": 18}, {"session_id": "bud", "agent_id": "a1"})[0] == 0)
p = subprocess.run([PY, os.path.join(SRC, "hook_session.py")], input=json.dumps({"cwd": REPO, "session_id": "bud", "source": "compact"}),
                   capture_output=True, text=True, env=env)
check("SessionStart 注入概览并在 compact 后重置预算", "ctxguard" in p.stdout and "svc/" in p.stdout and
      hook("Read", {"file_path": repo_path("svc/orders.py"), "offset": 1, "limit": 18}, {"session_id": "bud"})[0] == 0, p.stdout + p.stderr)
os.remove(cfgp)

# ---------------- CLI / 工具
def cli(*a):
    return subprocess.run([PY, os.path.join(SRC, "index.py"), *a], capture_output=True, text=True, env=env).stdout


out = cli("search", "method_")
check("search 多处命中 → 文件→段 分布", "svc/orders.py" in out and "段:" in out and not __import__("re").search(r"\n  \d+: ", out), out)
out = cli("search", "method_2_3")
check("search 单处命中 → 直接给行", "356: def method_2_3" in out, out)
out = cli("search", "method_2_3", "--path", "svc/orders.py")
check("search 缩小范围后给出具体行", "def method_2_3" in out, out)
out = cli("search", "calc_7", "--glob", "*.c", "--mode", "lines")
check("search glob/lines", "native/calc.c" in out and "calc_7" in out, out)
out = cli("symbols", "Run1")
check("symbols 跨语言(Go)", "cmd/server.go" in out, out)
out = cli("symbols", "util3")
check("symbols 跨语言(JS)", "web/utils.js" in out, out)
out = cli("symbols", "", "--path", "docs/whitepaper.md", "--limit", "5")
check("symbols 列出 Markdown 标题", "白皮书" in out or "主题1" in out, out)
out = cli("read", "svc/orders.py")
check("read 无区间且文件大 → 拒绝+目录", out.startswith("REFUSED") and "Handler0" in out, out)
out = cli("read", "svc/orders.py", "--start", "26", "--end", "43")
check("read 区间 → 带行号 + 下一段提示", "26\t" in out and "下一段" in out, out)
out = cli("read", "svc/orders.py", "--start", "1", "--end", "900")
check("read 超大区间 → 拒绝", out.startswith("REFUSED"), out)
out = cli("read", "data/dump.json")
check("read 压缩 JSON → 指向视图", out.startswith("REFUSED") and "视图" in out, out)
out = cli("outline", "data/dump.json")
check("outline 压缩 JSON → 视图目录", "可读视图" in out and "段" in out, out)
out = cli("tree", "svc")
check("tree 子目录", "orders.py" in out, out)

# ---------------- MCP 服务
srv = subprocess.Popen([PY, os.path.join(SRC, "server.py")], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, env=env, cwd=REPO)
srv_in, srv_out = pipes(srv)


def rpc(i, method, params=None):
    srv_in.write(json.dumps({"jsonrpc": "2.0", "id": i, "method": method, "params": params or {}}) + "\n")
    srv_in.flush()
    return json.loads(srv_out.readline())


r = rpc(1, "initialize", {"protocolVersion": "2025-03-26"})
check("MCP initialize", r["result"]["serverInfo"]["name"] == "ctxguard", str(r))
srv_in.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
srv_in.flush()
r = rpc(2, "tools/list")
check("MCP tools/list 含 5 个工具", {t["name"] for t in r["result"]["tools"]} == {"tree", "outline", "read", "search", "symbols"}, str(r)[:200])
r = rpc(3, "tools/call", {"name": "read", "arguments": {"path": "svc/orders.py"}})
check("MCP read 整份大文件 → isError + 目录", r["result"]["isError"] and "Handler0" in r["result"]["content"][0]["text"], str(r)[:200])
r = rpc(4, "tools/call", {"name": "read", "arguments": {"path": "svc/orders.py", "start": 1, "end": 25}})
check("MCP read 区间", not r["result"]["isError"] and "(文件头)" not in r["result"]["content"][0]["text"][:5], str(r)[:200])
r = rpc(5, "tools/call", {"name": "search", "arguments": {"pattern": "Worker3"}})
check("MCP search", "core/worker.rs" in r["result"]["content"][0]["text"], str(r)[:300])
r = rpc(6, "tools/call", {"name": "nope", "arguments": {}})
check("MCP 未知工具 → isError", r["result"]["isError"])
r = rpc(7, "unknown/method")
check("MCP 未知方法 → error", "error" in r)
srv_in.close()
srv.wait(timeout=10)

# ---------------- 代理
fake = os.path.join(tempfile.mkdtemp(), "fake_mcp.py")
with open(fake, "w") as f:
    f.write('''
import json, sys
for line in sys.stdin:
    m = json.loads(line)
    if "id" not in m: continue
    if m["method"] == "tools/call":
        n = m["params"]["name"]
        txt = json.dumps({"review": [{"file": "f%d.py" % i, "comment": "问题说明 " * 20} for i in range(2500)]}) if n == "big" else "ok small"
        res = {"content": [{"type": "text", "text": txt}], "structuredContent": {"x": txt}}
    else:
        res = {"echo": m["method"]}
    print(json.dumps({"jsonrpc": "2.0", "id": m["id"], "result": res}), flush=True)
''')
px = subprocess.Popen([PY, os.path.join(SRC, "proxy.py"), "--", PY, fake], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, env=env, cwd=REPO)
px_in, px_out = pipes(px)


def prpc(i, method, params=None):
    px_in.write(json.dumps({"jsonrpc": "2.0", "id": i, "method": method, "params": params or {}}) + "\n")
    px_in.flush()
    return json.loads(px_out.readline())


r = prpc(1, "initialize")
check("代理透传普通消息", r["result"]["echo"] == "initialize", str(r))
r = prpc(2, "tools/call", {"name": "small"})
check("代理：小结果原样返回", r["result"]["content"][0]["text"] == "ok small" and "structuredContent" in r["result"], str(r))
r = prpc(3, "tools/call", {"name": "big"})
txt = r["result"]["content"][0]["text"]
check("代理：大结果转存，只返回目录", "已转存" in txt and len(txt) < 6000 and "structuredContent" not in r["result"], txt[:300])
stash = os.path.join(REPO, ".ctxindex", "stash")
check("代理：转存文件存在且很大", os.path.isdir(stash) and any(os.path.getsize(os.path.join(stash, f)) > 100000 for f in os.listdir(stash)))
px_in.close()
px.wait(timeout=10)

fails = [n for n, ok in results if not ok]
print(f"\n{len(results) - len(fails)}/{len(results)} 通过")
sys.exit(1 if fails else 0)
