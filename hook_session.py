#!/usr/bin/env python3
"""ctxguard SessionStart hook：增量更新索引，并把项目目录概览注入上下文（索引优先）。
source=clear/compact 时重置主线程累计读取预算。任何异常都静默退出，绝不影响会话启动。"""
import contextlib
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lib  # noqa: E402
import tools  # noqa: E402


def main():
    lib.set_stdout_encoding()
    try:
        d = tools.read_stdin_json() or {}
        root, cfg = tools.project_context(d.get("cwd"))
        if d.get("source") in ("clear", "compact"):
            sid = d.get("session_id") or "default"
            p = os.path.join(root, lib.INDEX_DIR, "session-%s.json" % "".join(c if c.isalnum() or c in "_-" else "_" for c in sid))
            with contextlib.suppress(OSError):
                os.remove(p)
        for w in tools.CONFIG_WARNINGS:      # 配置写坏了，退回了默认值——让用户看见
            print(w)
        res = lib.build(root, time_limit=25)
        print("[ctxguard] 项目已建立索引（%d 个文件）。读取纪律：大文件不会被整份读取；"
              "先 tree → outline(path) → read(path,start,end)，用 search / symbols 定位。目录概览：" % res["total"])
        print(tools.overview(root))
    except Exception:  # noqa: BLE001
        return


if __name__ == "__main__":
    main()
