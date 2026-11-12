"""pytest 共享夹具。

两支脚本测试各跑**一次**，结果同时供两处使用：
- test_scripts.py：断言全部检查项通过
- test_facts.py：从输出里读出检查项数量，核对文档里的数字

跑两遍既慢又没必要——脚本本身就是最贵的部分（要起 MCP 服务、造样例仓库）。
"""
import os
import subprocess
import sys

import pytest

# test_all.py 与 test_backends.py 是**独立脚本**（模块级就按 argv 跑完整流程），
# 不是 pytest 用例：直接 import 会因为缺少命令行参数而崩。它们由 test_scripts.py
# 以子进程方式驱动，这里让 pytest 跳过直接收集。
collect_ignore = ["test_all.py", "test_backends.py"]

SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = sys.executable
SCRIPTS = os.path.join(SRC, "tests")


@pytest.fixture(scope="session")
def sample_repo(tmp_path_factory):
    """多语言样例仓库。索引器用 git ls-files 列文件，所以必须是 git 仓库。"""
    root = tmp_path_factory.mktemp("sample")
    subprocess.run([PY, os.path.join(SCRIPTS, "gen_sample.py"), str(root)],
                   check=True, capture_output=True, text=True)
    return str(root)


@pytest.fixture(scope="session")
def script_runs(sample_repo):
    """跑两支脚本测试各一次，返回 {名字: CompletedProcess}。"""
    runs = {}
    for name, script, args in (("test_all", "test_all.py", [SRC, sample_repo]),
                               ("test_backends", "test_backends.py", [SRC])):
        runs[name] = subprocess.run([PY, os.path.join(SCRIPTS, script), *args],
                                    capture_output=True, text=True)
    return runs
