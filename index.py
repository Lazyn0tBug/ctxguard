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
    for tool in tools.TOOLS:          # 子命令与参数由注册表生成，不另写一份
        p = sub.add_parser(tool.name, help=tool.summary)
        for arg in tool.args:
            if not arg.flag:
                if arg.required:
                    p.add_argument(arg.name, help=arg.description)
                else:
                    p.add_argument(arg.name, nargs="?", default=arg.default, help=arg.description)
            elif arg.kind == "int":
                p.add_argument("--" + arg.flag, dest=arg.name, type=int,
                               default=arg.default, help=arg.description)
            elif arg.enum:
                p.add_argument("--" + arg.flag, dest=arg.name, choices=list(arg.enum),
                               default=arg.default, help=arg.description)
            else:
                p.add_argument("--" + arg.flag, dest=arg.name,
                               default=arg.default, help=arg.description)
    sub.add_parser("doctor")
    c = sub.add_parser("compare")
    c.add_argument("path")
    c.add_argument("--backend")
    a = ap.parse_args()

    root = tools.find_root()
    cfg = tools.load_config(root)
    if a.cmd == "build":
        res = lib.build(root, time_limit=a.time_limit, force=a.force)
        if not a.quiet:
            print(f"索引完成：{res['done']}/{res['total']} 个文件，本次更新 {res['changed']} 个"
                  + ("（未完成，下次继续）" if res["partial"] else ""))
    elif a.cmd in tools.BY_NAME:
        tool = tools.BY_NAME[a.cmd]
        values = {arg.name: getattr(a, arg.name) for arg in tool.args}
        print(tools.call_tool(tool, root, values))
    elif a.cmd == "doctor":
        import diag
        text, problems = diag.doctor(root, cfg)
        print(text)
        sys.exit(1 if problems else 0)
    elif a.cmd == "compare":
        import diag
        print(diag.compare(root, a.path, a.backend))


if __name__ == "__main__":
    main()
