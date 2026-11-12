"""pytest 入口：把两支脚本测试包成 pytest 用例。

ctxguard 的端到端测试原本是独立脚本（需要先手工造样例仓库），这里用 fixture 自动准备，
让 `uv run pytest` 一条命令跑完全部检查；
脚本内部逐条 check 的结果仍保留在输出里，失败时整段打印。
"""


def test_end_to_end(script_runs):
    """索引 / hook / 预算 / MCP / 代理。"""
    p = script_runs["test_all"]
    assert p.returncode == 0, p.stdout + p.stderr


def test_backends(script_runs):
    """后端注册表 / 签名 / 回退 / 插件安全（装了 tree-sitter 会追加真实语法自检）。"""
    p = script_runs["test_backends"]
    assert p.returncode == 0, p.stdout + p.stderr
