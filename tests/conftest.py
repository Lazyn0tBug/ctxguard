"""pytest 收集配置。

tests/test_all.py 与 tests/test_backends.py 是**独立脚本**（模块级就按 argv 跑完整流程），
不是 pytest 用例：直接 import 会因为缺少命令行参数而崩。它们由 tests/test_scripts.py
以子进程方式驱动，这里让 pytest 跳过直接收集。
"""
collect_ignore = ["test_all.py", "test_backends.py"]
