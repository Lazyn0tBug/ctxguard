#!/usr/bin/env python3
"""ctxguard 后端：Python 标准库 ast。零依赖，精确，可在任何机器上测试，也是 tree-sitter 后端的对照基准。"""
import ast
import sys

NAME = "pyast"
VERSION = "1"

_FUNC = (ast.FunctionDef, ast.AsyncFunctionDef)
_FLOW = tuple(t for t in (ast.If, ast.Try, ast.With, ast.AsyncWith, ast.For, ast.AsyncFor, ast.While,
                          getattr(ast, "TryStar", None), getattr(ast, "Match", None)) if t is not None)


def signature_extra():
    return "py%d.%d" % sys.version_info[:2]


def supports(ext):
    return ext in (".py", ".pyi", ".pyw")


def _sig(lines, node):
    a = node.lineno - 1
    b = node.body[0].lineno - 1 if node.body else a
    seg = [lines[a]] if b <= a else lines[a:b]
    seg = [x for x in seg if not x.strip().startswith("#")]
    return " ".join(" ".join(x.split()) for x in seg)[:120]


def declarations(text, ext, opts):
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return None
    lines = text.split("\n")
    desc_fn = bool(opts.get("descend_functions", False))
    max_level = int(opts.get("max_depth", 6))
    out = []

    def walk(body, level):
        for n in body:
            if isinstance(n, _FUNC) or isinstance(n, ast.ClassDef):
                start = min([n.lineno] + [d.lineno for d in n.decorator_list]) - 1
                out.append((start, level, _sig(lines, n), n.end_lineno - 1))
                if level < max_level and (isinstance(n, ast.ClassDef) or desc_fn):
                    walk(n.body, level + 1)
            elif isinstance(n, _FLOW):
                for field in ("body", "orelse", "finalbody"):
                    walk(getattr(n, field, None) or [], level)
                for h in getattr(n, "handlers", None) or []:
                    walk(h.body, level)
                for c in getattr(n, "cases", None) or []:
                    walk(c.body, level)

    walk(tree.body, 0)
    return out
