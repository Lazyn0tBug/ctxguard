#!/usr/bin/env python3
"""ctxguard 诊断：doctor（环境与自检）、compare（启发式与后端的差异对照）。"""
import os
import sys
import time

import backends
import lib
import tools


def _fmt_decl(d):
    s, lv, txt = d[0] + 1, d[1], d[2]
    e = (d[3] + 1) if len(d) > 3 and d[3] is not None else None
    return "%5d%s L%d %s%s" % (s, ("-%d" % e if e else "").ljust(7), lv, "  " * lv, txt[:90])


def doctor(root, cfg):
    out, problems = [], 0
    p = out.append
    p("== ctxguard doctor ==")
    p("项目根: %s" % root)
    p("Python: %s" % sys.version.split()[0])
    p("生效的后端（按优先级）: %s" % (", ".join(lib.BACKENDS) or "(无，仅内置启发式)"))
    if lib.BACKEND_IGNORED:
        p("!! 已忽略项目级配置里的非内置后端: %s —— 出于安全，插件只能来自 ~/.ctxguard.json 或环境变量 CTXGUARD_BACKENDS"
          % ", ".join(lib.BACKEND_IGNORED))
    p("后端选项: %s" % lib.BACKEND_OPTS)
    p("索引签名: %s" % lib.index_signature())
    p("")

    names = lib.BACKENDS or []
    if not names:
        p("未启用任何后端。启用方式：")
        p('  项目根 .ctxguard.json:  {"backends": ["treesitter", "pyast"]}   （或 "auto"）')
        p("  或环境变量:             CTXGUARD_BACKENDS=auto")
        p("下面仍会检查内置后端是否可用，便于在装依赖前后对比。")
        names = ["pyast", "treesitter"]
        p("")

    for name in names:
        mod = backends.load(name)
        if mod is None:
            problems += 1
            p("[%s] 加载失败: %s" % (name, backends.last_errors().get(name, "?")))
            continue
        extra = ""
        if hasattr(mod, "signature_extra"):
            try:
                extra = " (%s)" % mod.signature_extra()
            except Exception as ex:  # noqa: BLE001
                extra = " (signature_extra 出错: %s)" % ex
        p("[%s] 已加载 v%s%s" % (name, getattr(mod, "VERSION", "?"), extra))

        if name == "pyast":
            problems += _selftest_pyast(mod, p)
        elif name == "treesitter":
            problems += _selftest_ts(mod, p)
        else:
            p("   （自定义后端：未提供内置自检；用 compare 手动对照）")
        p("")

    log = backends.LOG_PATH
    if log and os.path.exists(log):
        p("最近的后端日志 (%s):" % os.path.relpath(log, root))
        with open(log, encoding="utf-8") as f:
            for line in f.read().splitlines()[-8:]:
                p("  " + line[:200])
    p("")
    p("结论: " + ("全部自检通过" if problems == 0 else "%d 项需要处理（见上面带 ✗ / 加载失败 的行）" % problems))
    return "\n".join(out), problems


def _check_sample(mod, lang, code, must, forbid, levels, ext, p):
    t0 = time.monotonic()
    try:
        decls = mod.declarations(code, ext, dict(lib.BACKEND_OPTS))
    except Exception as ex:  # noqa: BLE001
        p("   ✗ %-10s 解析出错: %s: %s" % (lang, type(ex).__name__, str(ex)[:150]))
        return 1
    ms = (time.monotonic() - t0) * 1000
    if decls is None:
        p("   ✗ %-10s 后端返回 None（解析器不可用或解析失败）" % lang)
        return 1
    decls = backends._finish(backends.validate(decls) or [], len(code.split("\n")))
    texts = [d[2] for d in decls]
    missing = [m for m in must if not any(m in t for t in texts)]
    extra = [f for f in forbid if any(f in t for t in texts)]
    badlv = []
    for sub, want in levels.items():
        got = [d[1] for d in decls if sub in d[2]]
        if got and want not in got:
            badlv.append("%s 期望层级 %d 实际 %s" % (sub, want, got))
        if not got:
            badlv.append("%s 未找到" % sub)
    if missing or extra or badlv:
        p("   ✗ %-10s 缺失=%s 不应出现=%s 层级问题=%s" % (lang, missing, extra, badlv))
        for d in decls[:12]:
            p("       " + _fmt_decl(d))
        return 1
    p("   ✓ %-10s %d 个声明, %.1fms" % (lang, len(decls), ms))
    return 0


def _selftest_pyast(mod, p):
    import backend_ts
    code, must, forbid, levels = backend_ts.SELFTEST["python"]
    return _check_sample(mod, "python", code, must, forbid, levels, ".py", p)


def _selftest_ts(mod, p):
    bad = 0
    ok_langs = 0
    shown = None
    for lang, (code, must, forbid, levels) in mod.SELFTEST.items():
        parser = mod.get_parser(lang)
        if parser is None:
            errs = mod.load_errors(lang)
            # 各语言的失败原因通常相同（依赖没装）：只完整打印第一次，其余写“同上”
            norm = [e.split(":", 1)[0] + ":" + e.split(":", 2)[1] + ":" + e.split(":")[2].split("'")[0] if e.count(":") >= 2 else e for e in errs]
            if shown is not None and norm == shown:
                p("   - %-10s 解析器不可用（原因同上）" % lang)
            else:
                shown = norm
                p("   - %-10s 解析器不可用:%s" % (lang, ("\n       " + "\n       ".join(errs)) if errs else " (未尝试)"))
            continue
        ok_langs += 1
        bad += _check_sample(mod, lang, code, must, forbid, levels, mod.SAMPLE_EXT[lang], p)
    if ok_langs == 0:
        p("   ✗ 没有任何语言的解析器可用。安装: pip install tree-sitter tree-sitter-language-pack")
        bad += 1
    return bad


def compare(root, path, backend=None):
    ab, rel = tools.resolve(root, path)
    if not os.path.isfile(ab):
        return "ERROR: 文件不存在：%s" % path
    ext = os.path.splitext(ab)[1].lower()
    lines = lib.read_lines(ab)
    text = "\n".join(lines)
    names = backends.expand(backend) if backend else (lib.BACKENDS or ["treesitter", "pyast"])
    out = ["== compare %s (%d 行, 后端候选: %s) ==" % (rel, len(lines), ", ".join(names))]

    heur = [(d[0], d[1], d[2]) for d in lib.find_decls(lines, ext)]
    be = backends.get_decls(text, ext, names, lib.BACKEND_OPTS, len(lines))
    if be is None:
        out.append("后端没有产出声明（不支持该扩展名 / 解析器不可用 / 解析失败 / 空结果回退）。原因见 `doctor` 与 .ctxindex/backend.log。")
        be = []
    out.append("启发式声明 %d 个；后端声明 %d 个。" % (len(heur), len(be)))

    hset = {d[0] for d in heur}
    bset = {d[0] for d in be}
    only_h = [d for d in heur if d[0] not in bset]
    only_b = [d for d in be if d[0] not in hset]
    out.append("")
    out.append("只在启发式里（可能是误报：字符串/注释里的伪声明、闭包等）: %d" % len(only_h))
    out += ["  " + _fmt_decl(d) for d in only_h[:25]]
    out.append("只在后端里（启发式漏掉的）: %d" % len(only_b))
    out += ["  " + _fmt_decl(d) for d in only_b[:25]]

    def outline(be_names):
        old = lib.BACKENDS
        lib.BACKENDS = be_names
        try:
            _, chunks, _ = lib.chunk_text(text, ext)
        finally:
            lib.BACKENDS = old
        return chunks

    for label, bn in (("启发式", []), ("后端", names)):
        ch = outline(bn)
        out.append("")
        out.append("[%s] 目录：%d 段，最大 %d tok" % (label, len(ch), max((c.tok for c in ch), default=0)))
        for c in ch[:30]:
            out.append("  %d-%d | ~%dtok | %s" % (c.s, c.e, c.tok, c.title[:90]))
        if len(ch) > 30:
            out.append("  …… 另有 %d 段" % (len(ch) - 30))
    return "\n".join(out)
