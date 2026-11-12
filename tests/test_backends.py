#!/usr/bin/env python3
"""ctxguard 后端测试。用法：test_backends.py <ctxguard目录>

分三部分：
  A. 框架与 pyast：在任何机器上都能跑（本文件的主体）
  B. tree-sitter 后端的“遍历逻辑”：用与真实语法同形状的假节点测，不依赖 tree-sitter 本体
  C. 真实 tree-sitter：只有装了依赖才跑，否则跳过（这是你在目标机器上要关注的部分）
"""
import ast
import contextlib
import json
import os
import subprocess
import sys
import tempfile
import types

SRC = os.path.abspath(sys.argv[1])
sys.path.insert(0, SRC)
import backend_pyast  # noqa: E402
import backend_ts  # noqa: E402
import backends  # noqa: E402
import lib  # noqa: E402
import tools  # noqa: E402

results = []


def check(name, cond, detail=""):
    results.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name + ((" | " + str(detail)[:400].replace("\n", " ⏎ ")) if (detail and not cond) else ""))


def skip(name, why):
    print("SKIP " + name + " | " + why)


@contextlib.contextmanager
def cfg(names, opts=None, chunk_lines=None, loaded=True):
    old = (lib.BACKENDS, lib.BACKEND_OPTS, lib.CONFIG_LOADED, lib.MAX_CHUNK_LINES)
    lib.BACKENDS = list(names)
    lib.BACKEND_OPTS = dict({"descend_functions": False, "max_depth": 6}, **(opts or {}))
    lib.CONFIG_LOADED = loaded
    if chunk_lines:
        lib.MAX_CHUNK_LINES = chunk_lines
    try:
        yield
    finally:
        lib.BACKENDS, lib.BACKEND_OPTS, lib.CONFIG_LOADED, lib.MAX_CHUNK_LINES = old


def chunks(text, ext, names, **kw):
    with cfg(names, **kw):
        return lib.chunk_text(text, ext)[1]


def covers(text, ch):
    n = len(lib.split_lines(text))
    if not ch:
        return n == 0
    pos = 1
    for c in sorted(ch, key=lambda c: c.s):
        if c.s != pos or c.e < c.s:
            return False
        pos = c.e + 1
    return pos == n + 1


# ====================================================================== A. 框架与 pyast
SAMPLE = '''import os

def real_func(a):
    """说明里有示例：
    def fake_in_docstring(x):
        return x
    class NotAClass:
    """
    msg = """
def also_fake():
    pass
"""
    return a

@decorator(
    arg=1,
)
def decorated(a,
              b):
    x = 1
    def inner_closure():
        return 2
    return x

class Service:
    async def method_a(self):
        pass

    class Nested:
        def deep(self):
            pass

if True:
    def conditional():
        pass

TOP = 1
print(TOP)

try:
    def in_try():
        pass
except Exception:
    def in_except():
        pass
''' + "\n".join("    pad%d = %d" % (i, i) for i in range(0)) + "\n"

d = backend_pyast.declarations(SAMPLE, ".py", {"descend_functions": False, "max_depth": 6}) or []
texts = [x[2] for x in d]
check("pyast: 不再把 docstring/字符串里的 def/class 当声明",
      not any("fake" in t or "NotAClass" in t for t in texts), texts)
check("pyast: 真声明齐全",
      all(any(n in t for t in texts) for n in ["real_func", "decorated", "inner_closure"][:2] + ["Service", "method_a", "Nested", "deep", "conditional", "in_try", "in_except"]), texts)
check("pyast: 默认不把闭包当声明", not any("inner_closure" in t for t in texts))
d2 = backend_pyast.declarations(SAMPLE, ".py", {"descend_functions": True, "max_depth": 6}) or []
check("pyast: descend_functions=True 时闭包出现且层级为 1",
      any("inner_closure" in x[2] and x[1] == 1 for x in d2), d2)
lines = SAMPLE.split("\n")
dec = next(x for x in d if "decorated" in x[2])
check("pyast: 多行装饰器归入声明起始行", lines[dec[0]].startswith("@decorator("), (dec, lines[dec[0]]))
check("pyast: 多行签名完整", "decorated(a, b):" in dec[2].replace("  ", " "), dec)
check("pyast: 嵌套类成员层级正确",
      next(x for x in d if "deep" in x[2])[1] == 2 and next(x for x in d if "method_a" in x[2])[1] == 1)
check("pyast: 语法错误返回 None（回退启发式）", backend_pyast.declarations("def broken(:\n  pass\n", ".py", {}) is None)

ch = chunks(SAMPLE, ".py", ["pyast"], chunk_lines=8)
check("接入分块器：区间不重叠且完整覆盖", covers(SAMPLE, ch), [(c.s, c.e) for c in ch])
check("接入分块器：标题里没有伪声明", not any("fake" in c.title or "NotAClass" in c.title for c in ch), [c.title for c in ch])
dec_chunk = next((c for c in ch if "decorated" in c.title), None)
check("接入分块器：装饰器所在行与函数在同一段",
      dec_chunk is not None and dec_chunk.s <= SAMPLE.split("\n").index("@decorator(") + 1 <= dec_chunk.e, dec_chunk)
check("接入分块器：声明之间的顶层代码单独成段（(其他代码)）",
      any("TOP = 1" in "\n".join(lines[c.s - 1:c.e]) and "其他代码" in c.title for c in chunks(SAMPLE, ".py", ["pyast"], chunk_lines=5)),
      [(c.s, c.e, c.title) for c in chunks(SAMPLE, ".py", ["pyast"], chunk_lines=5)])

# ---- 不变量：任意真实文件，开/关后端，段区间都必须不重叠且完整覆盖
real = []
for root_dir in ("/opt/node22/lib/node_modules/npm/lib", SRC):
    for dp, dn, fn in os.walk(root_dir):
        dn[:] = [x for x in dn if x not in ("node_modules", "__pycache__")]
        for f in fn:
            if os.path.splitext(f)[1] in (".js", ".py", ".md"):
                real.append(os.path.join(dp, f))
bad = []
for fp in real[:150]:
    with open(fp, encoding="utf-8", errors="replace") as f:
        t = f.read()
    ext = os.path.splitext(fp)[1]
    for names in ([], ["pyast"], ["treesitter", "pyast"]):
        if not covers(t, chunks(t, ext, names)):
            bad.append((fp, names))
check("不变量：%d 个真实文件 × 3 种配置，分段完整覆盖且不重叠" % min(len(real), 150), not bad, bad[:3])

# ---- 回退：与不开后端逐字一致
npm_js = "/opt/node22/lib/node_modules/npm/lib/commands/ls.js"
if os.path.exists(npm_js):
    with open(npm_js, encoding="utf-8") as f:
        text_js = f.read()
else:
    text_js = "\n".join("function f%d() {\n  return %d;\n}\n" % (i, i) for i in range(60))
base = [(c.s, c.e, c.title) for c in chunks(text_js, ".js", [])]
for label, names in (("未知后端名", ["no_such_backend"]), ("后端不支持该扩展名", ["pyast"]),
                     ("treesitter 未装依赖", ["treesitter"])):
    got = [(c.s, c.e, c.title) for c in chunks(text_js, ".js", names)]
    check("回退一致：%s" % label, got == base)

tmp = tempfile.mkdtemp()
plug = os.path.join(tmp, "plug.py")


def write_plugin(body):
    with open(plug, "w") as f:
        f.write(body)
    backends._cache.clear()
    backends._sig_one.cache_clear()


for label, body in (
        ("后端抛异常", "NAME='p';VERSION='1'\ndef supports(e): return True\ndef declarations(t,e,o): raise RuntimeError('boom')\n"),
        ("后端返回 None", "NAME='p';VERSION='1'\ndef supports(e): return True\ndef declarations(t,e,o): return None\n"),
        ("后端返回非法格式", "NAME='p';VERSION='1'\ndef supports(e): return True\ndef declarations(t,e,o): return 'abc'\n"),
        ("后端返回空列表且文件较大", "NAME='p';VERSION='1'\ndef supports(e): return True\ndef declarations(t,e,o): return []\n"),
        ("缺少必需成员", "NAME='p'\n")):
    write_plugin(body)
    got = [(c.s, c.e, c.title) for c in chunks(text_js, ".js", ["plugin:" + plug])]
    check("回退一致：" + label, got == base)

for label, ret in (("字符串", "'abc'"), ("元素缺字段", "[(1,)]"), ("层级非数字", "[(0,'x','t',None)]")):
    write_plugin("NAME='p';VERSION='1'\ndef supports(e): return True\ndef declarations(t,e,o): return %s\n" % ret)
    check("校验：小文件上后端返回非法格式（%s）→ 视为不可用，回退" % label,
          backends.get_decls("def a():\n  pass\n", ".py", ["plugin:" + plug], {}, 2) is None)

write_plugin("NAME='p';VERSION='1'\ndef supports(e): return e=='.conf'\n"
             "def declarations(t,e,o):\n    return [(i,0,l.strip(),None) for i,l in enumerate(t.split('\\n')) if l.startswith('SECTION')]\n")
conf = "".join("SECTION alpha%d\n" % i + "k = v\n" * 30 for i in range(5))
cc = chunks(conf, ".conf", ["plugin:" + plug], chunk_lines=40)
check("插件：自定义声明生效并进入目录", any("SECTION alpha3" in c.title for c in cc) and covers(conf, cc), [c.title for c in cc][:6])
write_plugin("NAME='p';VERSION='1'\ndef supports(e): return True\ndef declarations(t,e,o): return []\n")
check("插件：小文件返回空列表被接受（不回退）", backends.get_decls("a\nb\n", ".x", ["plugin:" + plug], {}, 2) == [])

# ---- 配置与安全
proj = tempfile.mkdtemp()
home = tempfile.mkdtemp()
os.environ["HOME"] = home
os.environ.pop("CTXGUARD_BACKENDS", None)
with open(os.path.join(proj, ".ctxguard.json"), "w") as f:
    json.dump({"backends": ["plugin:/tmp/evil.py", "pyast", "some.module"]}, f)
c = tools.load_config(proj)
check("安全：项目级配置只接受内置后端名", lib.BACKENDS == ["pyast"] and set(lib.BACKEND_IGNORED) == {"plugin:/tmp/evil.py", "some.module"}, (lib.BACKENDS, lib.BACKEND_IGNORED))
with open(os.path.join(home, ".ctxguard.json"), "w") as f:
    json.dump({"backends": ["plugin:" + plug]}, f)
os.remove(os.path.join(proj, ".ctxguard.json"))
tools.load_config(proj)
check("用户级配置允许 plugin:", ["plugin:" + plug] == lib.BACKENDS, lib.BACKENDS)
os.environ["CTXGUARD_BACKENDS"] = "auto"
tools.load_config(proj)
check("环境变量优先且 auto 展开为 [treesitter, pyast]", lib.BACKENDS == ["treesitter", "pyast"], lib.BACKENDS)
os.environ["CTXGUARD_BACKENDS"] = "off"
tools.load_config(proj)
check("环境变量 off 关闭全部后端", lib.BACKENDS == [], lib.BACKENDS)
del os.environ["CTXGUARD_BACKENDS"]
os.remove(os.path.join(home, ".ctxguard.json"))
tools.load_config(proj)
check("无配置时默认关闭", lib.BACKENDS == [])
check("expand: 逗号/列表/重复/off", backends.expand("pyast, pyast") == ["pyast"] and backends.expand(["auto", "pyast"]) == ["treesitter", "pyast"]
      and backends.expand("pyast,off") == [] and backends.expand(None) == [])

# ---- 索引签名：后端/参数变化使旧索引失效
repo = tempfile.mkdtemp()
subprocess.run(["git", "init", "-q"], cwd=repo)
with open(os.path.join(repo, "a.py"), "w") as f:
    f.write(SAMPLE)
with open(os.path.join(repo, "b.py"), "w") as f:
    f.write("def f():\n    return 1\n")
subprocess.run(["git", "add", "-A"], cwd=repo)
with cfg([]):
    r1 = lib.build(repo)
    r2 = lib.build(repo)
with cfg(["pyast"]):
    r3 = lib.build(repo)
    r4 = lib.build(repo)
with cfg(["pyast"], opts={"descend_functions": True}):
    r5 = lib.build(repo)
with cfg([]):
    r6 = lib.build(repo)
check("索引签名：首次全量、再次增量为 0", r1["changed"] == 2 and r2["changed"] == 0, (r1, r2))
check("索引签名：启用后端后自动重建，随后增量为 0", r3["changed"] == 2 and r4["changed"] == 0, (r3, r4))
check("索引签名：后端选项变化后重建；关闭后端后重建", r5["changed"] == 2 and r6["changed"] == 2, (r5, r6))
with cfg(["pyast"], loaded=False):       # 未加载配置的进程（如只读代理）不应清库
    con = lib.open_db(repo)
    n = con.execute("SELECT count(*) FROM files").fetchone()[0]
    con.close()
check("未加载配置的进程不会清空索引", n == 2, n)

# ---- 符号：后端声明写入 symbols 表
with cfg(["pyast"]):
    lib.build(repo, force=True)
    con = lib.open_db(repo)
    syms = [r[0] for r in con.execute("SELECT name FROM symbols WHERE path='a.py'")]
    con.close()
check("symbols 表使用后端的签名文本", any("decorated(a, b):" in x for x in syms) and not any("fake" in x for x in syms), syms)

# ---- 文档标题分派：.rst / .adoc / .org / 其余按 Markdown
def crumbs(ext, text):
    """doc_pieces 切出的标题面包屑（不含 "(开头)" 段）。"""
    pieces, _ = lib.doc_pieces(lib.split_lines(text), ext)
    return [c for _, _, c in pieces]


check("文档标题：RST 下划线式标题，含层级",
      crumbs(".rst", "运维手册\n========\n\n正文。\n\n故障处理\n--------\n\n正文。\n")
      == ["运维手册", "运维手册 > 故障处理"], crumbs(".rst", "运维手册\n========\n"))
check("文档标题：AsciiDoc 的 = 与 ==",
      crumbs(".adoc", "= 部署指南\n\n正文。\n\n== 环境准备\n\n正文。\n")
      == ["部署指南", "部署指南 > 环境准备"], crumbs(".adoc", "= 部署指南\n"))
check("文档标题：Org 的 * 与 **",
      crumbs(".org", "* 会议纪要\n\n正文。\n\n** 讨论要点\n\n正文。\n")
      == ["会议纪要", "会议纪要 > 讨论要点"], crumbs(".org", "* 会议纪要\n"))
check("文档标题：其余扩展名按 Markdown 处理",
      crumbs(".md", "# 标题\n\n正文。\n\n## 小节\n\n正文。\n") == ["标题", "标题 > 小节"],
      crumbs(".md", "# 标题\n"))

# ====================================================================== B. tree-sitter 遍历逻辑（假节点）
class N:
    def __init__(self, type_, start, end, children=(), fields=None):
        self.type, self.start_point, self.end_point = type_, start, end
        self.children, self.fields, self.is_named, self.has_error = list(children), dict(fields or {}), True, False

    @property
    def named_children(self):
        return [c for c in self.children if c.is_named]

    def child_by_field_name(self, name):
        return self.fields.get(name)


def _ident(n):
    return N("identifier", (n.lineno - 1, n.col_offset), (n.lineno - 1, n.col_offset + 1))


def _block(stmts):
    kids = [_conv(x) for x in stmts]
    return N("block", kids[0].start_point, kids[-1].end_point, kids)


def _conv(n):
    pos = ((n.lineno - 1, n.col_offset), (n.end_lineno - 1, n.end_col_offset))
    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        body = _block(n.body)
        t = "class_definition" if isinstance(n, ast.ClassDef) else "function_definition"
        node = N(t, pos[0], pos[1], [_ident(n), body], {"name": _ident(n), "body": body})
        if n.decorator_list:
            decos = [N("decorator", (x.lineno - 1, max(x.col_offset - 1, 0)), (x.end_lineno - 1, x.end_col_offset)) for x in n.decorator_list]
            return N("decorated_definition", decos[0].start_point, node.end_point, decos + [node], {"definition": node})
        return node
    if isinstance(n, (ast.If, ast.Try, ast.With, ast.For, ast.While)):
        kids = []
        for fld in ("body", "orelse", "finalbody"):
            lst = getattr(n, fld, None) or []
            if lst:
                kids.append(_block(lst))
        for h in getattr(n, "handlers", None) or []:
            kids.append(_block(h.body))
        return N("compound_statement", pos[0], pos[1], kids)
    return N("expression_statement", pos[0], pos[1])


class FakePyParser:
    def parse(self, data):
        tree = ast.parse(data.decode("utf-8"))
        root = N("module", (0, 0), (len(data.split(b"\n")), 0), [_conv(x) for x in tree.body])
        return types.SimpleNamespace(root_node=root)


def install_fake_pack(parsers):
    mod = types.ModuleType("tree_sitter_language_pack")

    def get_parser(name):
        if name in parsers:
            return parsers[name]
        raise LookupError("no grammar " + name)
    mod.__dict__["get_parser"] = get_parser
    sys.modules["tree_sitter_language_pack"] = mod
    backend_ts._PARSERS.clear()
    backend_ts._LOAD_ERRORS.clear()


install_fake_pack({"python": FakePyParser()})
opts = {"descend_functions": False, "max_depth": 6}
for label, o in (("默认", opts), ("descend_functions", dict(opts, descend_functions=True)), ("max_depth=1", dict(opts, max_depth=1))):
    a = backend_pyast.declarations(SAMPLE, ".py", o)
    b = backend_ts.declarations(SAMPLE, ".py", o)
    check("tree-sitter 遍历逻辑（假节点）与 pyast 一致：" + label, a == b, ("pyast", a, "ts", b))
check("tree-sitter 后端：解析器缺失的语言返回 None", backend_ts.declarations("x", ".go", {}) is None)

# ---- 保护：节点预算 / 错误树 / 递归过深
_old_visits = backend_ts.MAX_VISITS
backend_ts.MAX_VISITS = 2
try:
    _r_budget = backend_ts.declarations(SAMPLE, ".py", opts)
finally:
    backend_ts.MAX_VISITS = _old_visits
check("tree-sitter 保护：节点数超预算 → 返回 None（交给回退）", _r_budget is None, _r_budget)


class _FixedParser:
    def __init__(self, kids, has_error=False):
        self.kids, self.has_error = kids, has_error

    def parse(self, data):
        root = N("module", (0, 0), (3, 0), self.kids)
        root.has_error = self.has_error
        return types.SimpleNamespace(root_node=root)


def _mk_fn():
    body = N("block", (1, 0), (1, 8))
    return N("function_definition", (0, 0), (1, 8), [body], {"body": body})


_SRC1 = "def f():\n    pass\n"
install_fake_pack({"python": _FixedParser([], has_error=True)})
check("tree-sitter 保护：根节点有语法错误且没找到声明 → 返回 None",
      backend_ts.declarations(_SRC1, ".py", opts) is None)
install_fake_pack({"python": _FixedParser([_mk_fn()], has_error=True)})
_r_part = backend_ts.declarations(_SRC1, ".py", opts)
check("tree-sitter 保护：有语法错误但仍找到声明 → 采用部分结果",
      _r_part is not None and len(_r_part) == 1 and "def f" in _r_part[0][2], _r_part)


def _nest(depth):
    node = _mk_fn()
    for _ in range(depth):
        node = N("block", (0, 0), (1, 8), [node])
    return node


install_fake_pack({"python": _FixedParser([_nest(50)])})
_r_shallow = backend_ts.declarations(_SRC1, ".py", opts)
install_fake_pack({"python": _FixedParser([_nest(backend_ts.MAX_RECURSION + 50)])})
try:
    _r_deep = backend_ts.declarations(_SRC1, ".py", opts)
    _deep_err = None
except Exception as _ex:  # noqa: BLE001
    _r_deep, _deep_err = "raised", _ex
check("tree-sitter 保护：50 层嵌套能找到声明", _r_shallow is not None and len(_r_shallow) == 1, _r_shallow)
check("tree-sitter 保护：超过递归上限不再下探、不崩溃、不返回 None（更深的声明被略过）",
      _r_deep == [], (_r_deep, _deep_err))
install_fake_pack({"python": FakePyParser()})

# 手工搭 JS 形状：export 包装、箭头函数变量、类方法、无函数值的 const、字段箭头函数
JS = ["export function exported(a) {", "  return a;", "}", "const arrow = async () => {", "  return 1;", "};",
      "const plain = 3;", "class Store {", "  action(s) {", "    s.x = 1;", "  }", "  h = () => {", "    return 2;", "  };", "}",
      "module.exports = {", "  helper() {", "    return 3;", "  },", "};"]


def n(t, s, e, kids=(), **f):
    return N(t, (s, 0), (e, 1), kids, f)


fn_body = n("statement_block", 0, 2)
exp = n("export_statement", 0, 2, [n("function_declaration", 0, 2, [fn_body], body=fn_body)])
exp.fields["declaration"] = exp.children[0]
arrow_val = n("arrow_function", 3, 5)
lex1 = n("lexical_declaration", 3, 5, [n("variable_declarator", 3, 5, [arrow_val], value=arrow_val)])
lex2 = n("lexical_declaration", 6, 6, [n("variable_declarator", 6, 6, [n("number", 6, 6)], value=n("number", 6, 6))])
m_body = n("statement_block", 8, 10)
method = n("method_definition", 8, 10, [m_body], body=m_body)
fval = n("arrow_function", 11, 13)
field = n("field_definition", 11, 13, [fval], value=fval)
cls_body = n("class_body", 7, 14, [method, field])
cls = n("class_declaration", 7, 14, [cls_body], body=cls_body)
h_body = n("statement_block", 16, 18)
helper = n("method_definition", 16, 18, [h_body], body=h_body)
assign = n("expression_statement", 15, 19, [n("assignment_expression", 15, 19, [n("object", 15, 19, [helper])])])
program = n("program", 0, 19, [exp, lex1, lex2, cls, assign])


class FakeJsParser:
    def parse(self, data):
        return types.SimpleNamespace(root_node=program)


install_fake_pack({"javascript": FakeJsParser()})
js_text = "\n".join(JS)
got = backend_ts.declarations(js_text, ".js", opts)
want_starts = {0: 0, 3: 0, 7: 0, 8: 1, 11: 1, 16: 0}
check("tree-sitter 遍历（JS 形状）：export 包装/箭头函数变量/类成员/字段箭头/对象方法",
      got is not None and {g[0]: g[1] for g in got} == want_starts, got)
check("tree-sitter 遍历（JS 形状）：无函数值的 const 不算声明", got is not None and all(g[0] != 6 for g in got), got)
check("tree-sitter 遍历（JS 形状）：签名取到函数体之前", got is not None and got[0][2] == "export function exported(a) {" and got[0][3] == 2, got)

# 解析器加载顺序：第一种方式失败时尝试下一种
sys.modules.pop("tree_sitter_language_pack", None)
fake2 = types.ModuleType("tree_sitter_languages")
fake2.__dict__["get_parser"] = lambda name: FakePyParser()
sys.modules["tree_sitter_languages"] = fake2
backend_ts._PARSERS.clear()
_p = backend_ts.get_parser("python")
check("解析器加载：language-pack 缺失时回退到 tree_sitter_languages，并记录第一种方式的失败原因",
      _p is not None and any(e.startswith("via_pack") for e in backend_ts.load_errors("python")), backend_ts.load_errors("python"))
sys.modules.pop("tree_sitter_languages", None)
backend_ts._PARSERS.clear()
check("解析器加载：全部缺失时返回 None 并记录原因", backend_ts.get_parser("python") is None and len(backend_ts.load_errors("python")) == 3, backend_ts.load_errors("python"))

# ====================================================================== C. 真实 tree-sitter（装了才跑）
backend_ts._PARSERS.clear()
real_ok = {lang: backend_ts.get_parser(lang) is not None for lang in backend_ts.SELFTEST}
if not any(real_ok.values()):
    skip("真实 tree-sitter 自检", "未安装 tree-sitter 或语言包；在目标机器上运行 `index.py doctor` 与本测试")
else:
    for lang, (code, must, forbid, levels) in backend_ts.SELFTEST.items():
        if not real_ok[lang]:
            skip("真实 tree-sitter：" + lang, "该语言解析器不可用")
            continue
        ext = backend_ts.SAMPLE_EXT[lang]
        got = backend_ts.declarations(code, ext, dict(opts))
        texts = [g[2] for g in (got or [])]
        miss = [m for m in must if not any(m in t for t in texts)]
        extra = [f for f in forbid if any(f in t for t in texts)]
        lv_bad = [s for s, w in levels.items() if not any(s in g[2] and g[1] == w for g in (got or []))]
        check("真实 tree-sitter：%s 自检" % lang, got is not None and not miss and not extra and not lv_bad,
              {"缺失": miss, "不应出现": extra, "层级": lv_bad, "实际": texts})
    if real_ok.get("python"):
        a = backend_pyast.declarations(SAMPLE, ".py", opts) or []
        b = backend_ts.declarations(SAMPLE, ".py", opts) or []
        check("真实 tree-sitter(python) 与 pyast 的声明起点/层级/终点一致",
              bool(b) and [(x[0], x[1], x[3]) for x in a] == [(x[0], x[1], x[3]) for x in b],
              {"pyast": [(x[0], x[1], x[3]) for x in a], "ts": [(x[0], x[1], x[3]) for x in b]})

# ====================================================================== D. 命令行
env = dict(os.environ, CTXGUARD_ROOT=repo, CTXGUARD_BACKENDS="pyast")
r = subprocess.run([sys.executable, os.path.join(SRC, "index.py"), "doctor"], capture_output=True, text=True, env=env, cwd=repo)
check("doctor：可运行并列出 pyast 自检通过", "[pyast] 已加载" in r.stdout and "✓ python" in r.stdout, r.stdout[-400:] + r.stderr[-200:])
r = subprocess.run([sys.executable, os.path.join(SRC, "index.py"), "compare", "a.py"], capture_output=True, text=True, env=env, cwd=repo)
check("compare：列出启发式误报（docstring 里的伪声明）", "只在启发式里" in r.stdout and "fake_in_docstring" in r.stdout, r.stdout[:600])
r = subprocess.run([sys.executable, os.path.join(SRC, "index.py"), "compare", "nope.xyz"], capture_output=True, text=True, env=env, cwd=repo)
check("compare：文件不存在给出错误提示", "ERROR" in r.stdout)

fails = [n_ for n_, ok in results if not ok]
print("\n%d/%d 通过" % (len(results) - len(fails), len(results)))
sys.exit(1 if fails else 0)
