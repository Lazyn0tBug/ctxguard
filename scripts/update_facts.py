#!/usr/bin/env python3
"""同步文档里会变的数字（检查项数、模块数、行数、工具数）。

用法：
  python scripts/update_facts.py            # 算出来并就地改写文档
  python scripts/update_facts.py --check    # 只校验：陈旧或链接悬空则退出 1（CI / 闸门用）

改写规则与期望命中次数写在 scripts/facts.py 的 RULES 里——文档结构变了会明确报错，
而不是默默改错地方。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import facts  # noqa: E402


def main():
    check = "--check" in sys.argv
    data = facts.all_facts()
    print("算出的事实：")
    for k in sorted(data):
        print("  %-18s %s" % (k, data[k]))

    if check:
        problems = facts.verify(data)
        if problems:
            print("\n有问题：")
            for p in problems:
                print("  ✗", p)
            print("\n跑 `python scripts/update_facts.py` 更新文档。")
            sys.exit(1)
        print("\n文档里的数字都是最新的，链接也都有效 ✓")
        return

    summary, problems = facts.rewrite(data)
    for p in problems:
        print("  ✗", p)
    for s in summary:
        print(" ", s)
    if not summary:
        print("  文档本来就是最新的，无需改动")
    if problems:
        sys.exit(1)


if __name__ == "__main__":
    main()
