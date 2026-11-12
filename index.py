#!/usr/bin/env python3
"""ctxguard 命令行：与 MCP 工具同一套实现，方便人手检查索引质量。

用法：
  index.py build [--force] [--quiet]
  index.py tree [path] [--depth N]
  index.py outline <path>
  index.py read <path> [--start N] [--end N]
  index.py search <pattern> [--path P] [--mode auto|files|lines] [--glob G] [--limit N]
  index.py symbols [query] [--path P]
  index.py doctor                              检查后端依赖并运行逐语言自检
  index.py compare <path> [--backend NAME]     对照启发式与后端的声明/目录差异
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lib  # noqa: E402
import tools  # noqa: E402


def main():
    lib.set_stdout_encoding()
    ap = argparse.ArgumentParser(prog="ctxguard")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("--force", action="store_true")
    b.add_argument("--quiet", action="store_true")
    b.add_argument("--time-limit", type=float, default=None)
    t = sub.add_parser("tree")
    t.add_argument("path", nargs="?", default=".")
    t.add_argument("--depth", type=int, default=2)
    o = sub.add_parser("outline")
    o.add_argument("path")
    r = sub.add_parser("read")
    r.add_argument("path")
    r.add_argument("--start", type=int)
    r.add_argument("--end", type=int)
    s = sub.add_parser("search")
    s.add_argument("pattern")
    s.add_argument("--path", default=".")
    s.add_argument("--mode", default="auto", choices=["auto", "files", "lines"])
    s.add_argument("--glob")
    s.add_argument("--limit", type=int, default=40)
    y = sub.add_parser("symbols")
    y.add_argument("query", nargs="?", default="")
    y.add_argument("--path")
    y.add_argument("--limit", type=int, default=50)
    sub.add_parser("doctor")
    c = sub.add_parser("compare")
    c.add_argument("path")
    c.add_argument("--backend")
    a = ap.parse_args()

    root = tools.find_root()
    tools.load_config(root)
    if a.cmd == "build":
        res = lib.build(root, time_limit=a.time_limit, force=a.force)
        if not a.quiet:
            print(f"索引完成：{res['done']}/{res['total']} 个文件，本次更新 {res['changed']} 个"
                  + ("（未完成，下次继续）" if res["partial"] else ""))
    elif a.cmd == "tree":
        print(tools.t_tree(root, a.path, a.depth))
    elif a.cmd == "outline":
        print(tools.t_outline(root, a.path))
    elif a.cmd == "read":
        print(tools.t_read(root, a.path, a.start, a.end))
    elif a.cmd == "search":
        print(tools.t_search(root, a.pattern, a.path, a.mode, a.limit, a.glob))
    elif a.cmd == "doctor":
        import diag
        text, problems = diag.doctor(root, tools.load_config(root))
        print(text)
        sys.exit(1 if problems else 0)
    elif a.cmd == "compare":
        import diag
        print(diag.compare(root, a.path, a.backend))
    elif a.cmd == "symbols":
        print(tools.t_symbols(root, a.query, a.path, a.limit))


if __name__ == "__main__":
    main()
