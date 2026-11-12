#!/usr/bin/env python3
"""生成多语言样例仓库用于测试 ctxguard。用法：gen_sample.py <目标目录>"""
import json
import os
import random
import subprocess
import sys

root = sys.argv[1]
random.seed(7)


def w(rel, text):
    p = os.path.join(root, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        f.write(text)


body = lambda n, ind: "".join(f"{' ' * ind}x{i} = compute(a, b) + {i}  # step {i}\n" for i in range(n))

# Python：多个类，每类多个方法
py = '"""订单处理服务。"""\nimport os\nimport sys\n\n'
for c in range(6):
    py += f'class Handler{c}(Base):\n    """Handler{c}：处理第 {c} 类订单。"""\n\n'
    for m in range(8):
        py += f"    def method_{c}_{m}(self, a, b):\n        \"\"\"方法 {c}.{m} 的说明。\"\"\"\n" + body(14, 8) + "        return a\n\n"
py += "def main():\n" + body(30, 4)
w("svc/orders.py", py)

# JS：函数、箭头函数、类
js = "// 前端工具集\nimport x from 'y';\n\n"
for i in range(30):
    js += f"export function util{i}(a, b) {{\n" + "".join(f"  const v{k} = a + b * {k};\n" for k in range(12)) + "  return a;\n}\n\n"
for i in range(10):
    js += f"export const arrow{i} = async (req, res) => {{\n" + "".join(f"  await step({k});\n" for k in range(10)) + "};\n\n"
js += "class Store {\n" + "".join(f"  action{i}(state) {{\n" + "".join(f"    state.a{k} = {k};\n" for k in range(8)) + "  }\n\n" for i in range(15)) + "}\n"
w("web/utils.js", js)

# C：函数签名与花括号分行
c = "#include <stdio.h>\n#include <stdlib.h>\n\n"
for i in range(25):
    c += f"/* 计算第 {i} 项 */\nstatic int calc_{i}(int a, int b)\n{{\n" + "".join(f"    a += b * {k};\n" for k in range(14)) + "    return a;\n}\n\n"
w("native/calc.c", c)

# Go
g = "package main\n\nimport \"fmt\"\n\n"
for i in range(25):
    g += f"// Run{i} 执行任务。\nfunc (s *Server) Run{i}(ctx context.Context) error {{\n" + "".join(f"\ts.step({k})\n" for k in range(14)) + "\treturn nil\n}\n\n"
w("cmd/server.go", g)

# Rust
r = "use std::io;\n\n"
for i in range(6):
    r += f"pub struct Worker{i} {{ id: u32 }}\n\nimpl Worker{i} {{\n"
    for m in range(5):
        r += f"    pub fn run_{m}(&self) -> Result<(), Error> {{\n" + "".join(f"        step({k})?;\n" for k in range(10)) + "        Ok(())\n    }\n\n"
    r += "}\n\n"
w("core/worker.rs", r)

# Java
j = "package com.acme;\n\npublic class OrderService {\n"
for i in range(25):
    j += f"    @Override\n    public Result process{i}(Request req) throws Exception {{\n" + "".join(f"        log.info(\"s{k}\");\n" for k in range(12)) + "        return null;\n    }\n\n"
j += "}\n"
w("java/OrderService.java", j)

# 中文 Markdown 文档
md = "# 行业平台建设白皮书\n\n本文说明平台建设的背景、方法与落地路径。\n\n"
for ch in range(1, 7):
    md += f"## 第{ch}章 主题{ch}\n\n这一章讨论主题{ch}的核心问题。\n\n"
    for s in range(1, 5):
        md += f"### {ch}.{s} 小节\n\n" + "".join(f"这是第{ch}.{s}节的第{k}段论述，包含若干中文句子用于增加长度。" * 3 + "\n\n" for k in range(6))
w("docs/whitepaper.md", md)
w("docs/plain.txt", "".join(f"第 {i} 行普通文本内容，没有任何标题结构。\n" * 1 for i in range(900)))

# YAML 配置
y = "".join(f"service_{i}:\n  image: app:{i}\n  replicas: 3\n  env:\n" + "".join(f"    K{k}: v{k}\n" for k in range(10)) for i in range(40))
w("ops/stack.yaml", y)

# 压成一行的大 JSON
w("data/dump.json", json.dumps({"rows": [{"id": i, "name": f"user{i}", "tags": ["a", "b", "c"]} for i in range(6000)]}))

# SQL / Shell
w("db/schema.sql", "".join(f"CREATE TABLE t{i} (\n  id INT,\n" + "".join(f"  c{k} TEXT,\n" for k in range(12)) + "  PRIMARY KEY(id)\n);\n\n" for i in range(30)))
w("scripts/deploy.sh", "#!/bin/bash\n" + "".join(f"deploy_{i}() {{\n" + "".join(f"  echo step {k}\n" for k in range(12)) + "}\n\n" for i in range(20)))
w("README.md", "# demo\n\n小文件，可以整份读。\n")
w(".gitignore", "node_modules/\n")
os.makedirs(os.path.join(root, "node_modules/x"), exist_ok=True)
w("node_modules/x/big.js", "var a=1;\n" * 5000)
w("assets/logo.png", "\x89PNG\0\0binary")

subprocess.run(["git", "init", "-q"], cwd=root)
subprocess.run(["git", "add", "-A"], cwd=root)
print("sample repo at", root)
