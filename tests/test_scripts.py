"""pytest 入口：把两支脚本测试包成 pytest 用例。

ctxguard 的端到端测试原本是独立脚本（要先手工造样例仓库、逐条看 PASS/FAIL）。
这里用 fixture 自动准备样例仓库，让 `uv run pytest` 一条命令跑完全部检查；
脚本内部逐条 check 的结果仍保留在输出里，失败时整段打印。
"""
import os
import subprocess
import sys

import pytest

SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = sys.executable


@pytest.fixture(scope="session")
def sample_repo(tmp_path_factory):
    """多语言样例仓库。索引器用 git ls-files 列文件，所以必须是 git 仓库。"""
    root = tmp_path_factory.mktemp("sample")
    subprocess.run([PY, os.path.join(SRC, "tests", "gen_sample.py"), str(root)],
                   check=True, capture_output=True, text=True)
    return str(root)


def run_script(script, *args):
    return subprocess.run([PY, os.path.join(SRC, "tests", script), *args],
                          capture_output=True, text=True)


def test_end_to_end(sample_repo):
    """索引 / hook / 预算 / MCP / 代理：83 项检查。"""
    p = run_script("test_all.py", SRC, sample_repo)
    assert p.returncode == 0, p.stdout + p.stderr


def test_backends():
    """后端注册表 / 签名 / 回退 / 插件安全：54 项检查（装了 tree-sitter 会追加真实语法自检）。"""
    p = run_script("test_backends.py", SRC)
    assert p.returncode == 0, p.stdout + p.stderr
