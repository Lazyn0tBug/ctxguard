"""文档闸门：数字过期或链接悬空就红。

README 与 docs/ 里散落着"多少项检查""多少个模块""多少行代码"这类会变的事实。
靠人记着同步必然漏（这轮漏过好几次：83/54、137、2,814 行、"老 Python 可运行"）。

现在：
- 事实只由 scripts/facts.py 计算
- 数字由 scripts/update_facts.py 写进文档
- 这里负责发现它们过期了

检查项数量直接取自脚本的**实际输出**（脚本运行时才知道自己跑了多少项），
所以它跟真实检查数不会脱节。
"""
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))
import facts  # noqa: E402


def _counts(script_runs):
    counts = {}
    for name in ("test_all", "test_backends"):
        m = re.search(r"(\d+)/(\d+) 通过", script_runs[name].stdout)
        assert m, "没能从 %s 的输出里读出检查项数量" % name
        counts[name] = int(m.group(1))
    counts["tests_total"] = counts["test_all"] + counts["test_backends"]
    return counts


def test_docs_facts_are_current(script_runs):
    """文档里的数字 = 实际事实；链接也都指向真实文件。"""
    data = {**facts.static_facts(), **_counts(script_runs)}
    problems = facts.verify(data)
    assert not problems, "\n".join(problems) + "\n（跑 `python scripts/update_facts.py` 更新文档）"


def test_facts_rules_still_match_the_docs(script_runs):
    """RULES 里的期望命中次数必须与文档现状一致——文档结构变了要显式更新规则。"""
    data = {**facts.static_facts(), **_counts(script_runs)}
    for doc in facts.DOCS:
        path = os.path.join(facts.REPO, doc)
        with open(path, encoding="utf-8") as f:
            text = f.read()
        _, problems = facts.apply_facts(doc, text, data)
        assert not problems, "\n".join(problems)
