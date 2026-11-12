"""文档里那些会变的数字：唯一的计算入口。

README 与 docs/ 里散落着"多少项检查""多少个模块""多少行代码"这类事实——它们随代码变化，
靠人肉同步必然漏（这轮就漏过好几次）。这里的规则是：

  1. 事实只在这里算一次（static_facts / test_counts）
  2. 文档里的数字由 scripts/update_facts.py 按 RULES 就地改写
  3. RULES 里写死了每条规则**应当命中几次**——文档结构变了就报错，而不是默默改错地方
  4. tests/test_facts.py 是闸门：数字过期或链接悬空就红

不引入占位符：文档里保持可读的数字，脚本靠上下文锚点定位（"两支脚本测试（87 项端到端"
这种），不靠"第几个 N"。
"""
import os
import re
import subprocess
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

# 运行时模块（install.py 是安装器本身，不投放）
RUNTIME_MODULES = [
    "lib.py", "tools.py", "backend_ts.py", "hook_pre.py", "backends.py", "diag.py",
    "server.py", "proxy.py", "index.py", "backend_pyast.py", "hook_session.py", "install.py",
]
DEPLOYED = [m for m in RUNTIME_MODULES if m != "install.py"]
DOCS = ["README.md", "docs/rust-migration.md", "docs/troubleshooting.md", "docs/future.md"]


def _lines(path):
    with open(os.path.join(REPO, path), encoding="utf-8") as f:
        return sum(1 for _ in f)


def static_facts():
    """不跑测试就能算出来的事实。"""
    import tools

    facts = {
        "modules": len(RUNTIME_MODULES),
        "deployed_modules": len(DEPLOYED),
        "tools_count": len(tools.TOOLS),
    }
    for m in RUNTIME_MODULES:
        facts["loc_" + m[:-3]] = _lines(m)
    facts["loc_total"] = sum(_lines(m) for m in RUNTIME_MODULES)
    facts["loc_deployed"] = sum(_lines(m) for m in DEPLOYED)
    return facts


def test_counts():
    """跑两支脚本测试，从它们的输出里读出检查项数量（权威来源是运行时，不是静态计数）。"""
    out = {}
    with tempfile.TemporaryDirectory() as tmp:
        sample = os.path.join(tmp, "sample")
        subprocess.run([sys.executable, os.path.join(REPO, "tests", "gen_sample.py"), sample],
                       check=True, capture_output=True, text=True)
        for script in ("test_all.py", "test_backends.py"):
            p = subprocess.run([sys.executable, os.path.join(REPO, "tests", script), REPO, sample]
                               if script == "test_all.py" else
                               [sys.executable, os.path.join(REPO, "tests", script), REPO],
                               capture_output=True, text=True)
            m = re.search(r"(\d+)/(\d+) 通过", p.stdout)
            if not m:
                raise RuntimeError("没能从 %s 的输出里读出检查数：\n%s" % (script, p.stdout[-500:]))
            out["test_all" if script == "test_all.py" else "test_backends"] = int(m.group(1))
    out["tests_total"] = out["test_all"] + out["test_backends"]
    return out


# (文件, 正则（含一个数字组）, 事实名, 应当命中的次数, 数字的写法)
# 写法 "int" = 原样，"comma" = 千分位。
RULES = [
    ("docs/rust-migration.md", r"现有 (\d+) 项检查构成回归基线", "tests_total", 1, "int"),
    ("docs/rust-migration.md", r"兼容行为\*\*：(\d+) 项现有检查", "tests_total", 1, "int"),
    ("docs/rust-migration.md", r"现有 (\d+) 项检查需先适配", "tests_total", 1, "int"),
    ("README.md", r"两支脚本测试（(\d+) 项端到端", "test_all", 1, "int"),
    ("README.md", r"两支脚本测试（\d+ 项端到端 \+ (\d+) 项后端", "test_backends", 1, "int"),
    ("README.md", r"/tmp/sample      # (\d+) 项：索引", "test_all", 1, "int"),
    ("README.md", r"tests/test_backends\.py.*# (\d+) 项 \+ 装了 tree-sitter", "test_backends", 1, "int"),
    ("README.md", r"跑完 (\d+) 项检查", "tests_total", 1, "int"),
    ("docs/rust-migration.md", r"(\d+) 项测试全绿", "tests_total", 1, "int"),
    ("docs/rust-migration.md", r"(\d+) 项全绿", "tests_total", 1, "int"),
    ("docs/rust-migration.md", r"由 (\d+) 个平铺的 Python 模块组成", "modules", 1, "int"),
    ("docs/rust-migration.md", r"\| 模块规模 \| (\d+) 个运行时模块", "modules", 1, "int"),
    ("docs/rust-migration.md", r"保持不变（(\d+) 个平铺模块", "modules", 1, "int"),
    ("docs/rust-migration.md", r"\`tools/call\`，(\d+) 个工具", "tools_count", 1, "int"),
    ("docs/rust-migration.md", r"Python 模块组成（([\d,]+) 行", "loc_total", 1, "comma"),
    ("docs/rust-migration.md", r"\| 模块规模 \| \d+ 个运行时模块 / ([\d,]+) 行", "loc_total", 1, "comma"),
    ("docs/rust-migration.md", r"lib (\d+)、tools", "loc_lib", 1, "int"),
    ("docs/rust-migration.md", r"tools (\d+)、backend_ts", "loc_tools", 1, "int"),
    ("docs/rust-migration.md", r"backend_ts (\d+)、hook_pre", "loc_backend_ts", 1, "int"),
    ("docs/rust-migration.md", r"hook_pre (\d+)、install", "loc_hook_pre", 1, "int"),
    ("docs/rust-migration.md", r"install (\d+)", "loc_install", 1, "int"),
]


def _fmt(name, value, style="int"):
    """事实 → 文档里的写法。"""
    return f"{value:,}" if style == "comma" else str(value)


def apply_facts(doc, text, facts):
    """按 RULES 就地改写数字，返回 (新文本, 问题列表)。问题非空说明文档结构与规则不匹配。"""
    problems = []
    for rule_doc, pattern, fact, expected, style in RULES:
        if rule_doc != doc:
            continue
        if fact not in facts:
            problems.append("%s: 未知事实 %s" % (doc, fact))
            continue
        hits = list(re.finditer(pattern, text))
        if len(hits) != expected:
            problems.append("%s: 规则 %r 命中 %d 次（应为 %d 次）——文档结构变了，需要更新 scripts/facts.py"
                            % (doc, pattern, len(hits), expected))
            continue
        new = _fmt(fact, facts[fact], style)
        text = re.sub(pattern, lambda m, n=new: m.group(0).replace(m.group(1), n), text)
    return text, problems


def rewrite(facts):
    """改写全部文档，返回 (改动摘要, 问题列表)。"""
    summary, problems = [], []
    for doc in DOCS:
        path = os.path.join(REPO, doc)
        with open(path, encoding="utf-8") as f:
            old = f.read()
        new, probs = apply_facts(doc, old, facts)
        problems += probs
        if new != old:
            with open(path, "w", encoding="utf-8") as f:
                f.write(new)
            summary.append("%s 已更新" % doc)
    return summary, problems


def diff_facts(doc, text, facts):
    """列出文档里与事实不符的数字（不区分"过期"与"写错"，都指出来）。"""
    problems = []
    for rule_doc, pattern, fact, _expected, style in RULES:
        if rule_doc != doc or fact not in facts:
            continue
        want = _fmt(fact, facts[fact], style)
        for m in re.finditer(pattern, text):
            if m.group(1) != want:
                problems.append("%s: %s 应为 %s（文档里写的是 %s）" % (doc, fact, want, m.group(1)))
    return problems


def verify(facts):
    """只校验不改写：返回问题列表（数字过期 / 规则失配 / 链接悬空）。"""
    problems = []
    for doc in DOCS:
        path = os.path.join(REPO, doc)
        with open(path, encoding="utf-8") as f:
            text = f.read()
        _, probs = apply_facts(doc, text, facts)
        problems += probs
        problems += diff_facts(doc, text, facts)
    problems += check_links()
    return problems


def check_links():
    """文档里的相对链接是否都指向真实存在的文件。"""
    problems = []
    for doc in DOCS:
        path = os.path.join(REPO, doc)
        with open(path, encoding="utf-8") as f:
            text = f.read()
        for target in re.findall(r"\]\(([^)]+)\)", text):
            if target.startswith(("http://", "https://", "#")):
                continue
            full = os.path.join(os.path.dirname(path), target)
            if not os.path.exists(full):
                problems.append("%s: 链接指向不存在的文件 %s" % (doc, target))
    return problems


def all_facts():
    return {**static_facts(), **test_counts()}
