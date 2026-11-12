#!/usr/bin/env python3
"""ctxguard 后端：tree-sitter。表驱动，按语言配置“哪些节点算声明”。

【重要】本模块在编写环境里没有真实的 tree-sitter 可用，遍历逻辑用“形状相同的假节点”测过，
但对真实语法包的接口和节点类型名并未实测。请在装好依赖的机器上先跑：
    python3 .claude/ctxguard/index.py doctor
它会逐语言加载解析器、运行自检样例并指出哪些预期没有满足；再用
    python3 .claude/ctxguard/index.py compare <文件>
对照启发式与本后端的差异。需要调整时只改下面的 SPECS 表即可。

依赖（任选其一；按此顺序尝试）：
    pip install tree-sitter tree-sitter-language-pack
    pip install tree-sitter-languages               # 旧的合集包
    pip install tree-sitter tree-sitter-python tree-sitter-javascript ...   # 各语言单包
"""
import importlib

NAME = "treesitter"
VERSION = "1"        # 修改 SPECS / 遍历逻辑时递增，旧索引会自动重建
PACKAGES = ("tree-sitter", "tree-sitter-language-pack", "tree-sitter-languages")

MAX_VISITS = 400_000
MAX_RECURSION = 200

F, C, L = "function", "container", "leaf"
# 角色：F=函数类（默认不进入其内部），C=容器（成员算下一层），L=叶子（只记录自己）


def _lang(**kw):
    base = dict(decl={}, wrappers={}, special=(), need_body=(), skip_bodiless=())
    base.update(kw)
    return base


_JS_DECL = {"function_declaration": F, "generator_function_declaration": F,
            "class_declaration": C, "method_definition": F}
_JS_SPECIAL = ("lexical_declaration", "variable_declaration", "field_definition", "public_field_definition")
_C_DECL = {"function_definition": F, "type_definition": L, "preproc_function_def": L,
           "enum_specifier": L}
_JAVA_DECL = {"class_declaration": C, "interface_declaration": C, "enum_declaration": C,
              "record_declaration": C, "annotation_type_declaration": L,
              "method_declaration": F, "constructor_declaration": F, "compact_constructor_declaration": F}

SPECS = {
    "python": _lang(
        decl={"function_definition": F, "class_definition": C},
        wrappers={"decorated_definition": "definition"}),
    "javascript": _lang(
        decl=dict(_JS_DECL), special=_JS_SPECIAL,
        wrappers={"export_statement": "declaration"}),
    "typescript": _lang(
        decl=dict(_JS_DECL, abstract_class_declaration=C, interface_declaration=L, type_alias_declaration=L,
                  enum_declaration=L, function_signature=L, module=C, internal_module=C),
        special=_JS_SPECIAL,
        wrappers={"export_statement": "declaration", "ambient_declaration": None}),
    "go": _lang(
        decl={"function_declaration": F, "method_declaration": F, "type_declaration": L}),
    "rust": _lang(
        decl={"function_item": F, "function_signature_item": L, "struct_item": L, "enum_item": L,
              "union_item": L, "trait_item": C, "impl_item": C, "mod_item": C, "type_item": L,
              "macro_definition": L},
        skip_bodiless=("mod_item",)),
    "java": _lang(decl=dict(_JAVA_DECL)),
    "c": _lang(
        decl=dict(_C_DECL, struct_specifier=L, union_specifier=L),
        need_body=("struct_specifier", "union_specifier", "enum_specifier")),
    "cpp": _lang(
        decl=dict(_C_DECL, struct_specifier=C, union_specifier=C, class_specifier=C, namespace_definition=C),
        wrappers={"template_declaration": None},
        need_body=("struct_specifier", "union_specifier", "enum_specifier", "class_specifier")),
    "c_sharp": _lang(
        decl={"class_declaration": C, "struct_declaration": C, "interface_declaration": C,
              "record_declaration": C, "record_struct_declaration": C, "namespace_declaration": C,
              "enum_declaration": L, "method_declaration": F, "constructor_declaration": F,
              "destructor_declaration": F, "operator_declaration": F}),
    "ruby": _lang(decl={"class": C, "module": C, "method": F, "singleton_method": F}),
}
SPECS["tsx"] = SPECS["typescript"]

EXT_LANG = {
    ".py": "python", ".pyi": "python",
    ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".ts": "typescript", ".mts": "typescript", ".cts": "typescript", ".tsx": "tsx",
    ".go": "go", ".rs": "rust", ".java": "java",
    ".c": "c", ".h": "cpp", ".cc": "cpp", ".cpp": "cpp", ".cxx": "cpp", ".hpp": "cpp", ".hh": "cpp", ".hxx": "cpp",
    ".cs": "c_sharp", ".rb": "ruby",
}
FUNC_VALUES = {"arrow_function", "function_expression", "function", "generator_function"}

# 各安装方式使用的语言名/模块名差异
PACK_ALIASES = {"c_sharp": ("csharp", "c_sharp"), "tsx": ("tsx",)}
PKG_MODULE = {"typescript": ("tree_sitter_typescript", "language_typescript"),
              "tsx": ("tree_sitter_typescript", "language_tsx")}

_PARSERS = {}
_LOAD_ERRORS = {}


def signature_extra():
    from backends import pkg_version
    return pkg_version(*PACKAGES)


def supports(ext):
    return ext in EXT_LANG


# ------------------------------------------------------------------ 解析器加载
def _via_pack(lang):
    from tree_sitter_language_pack import get_parser
    last = None
    for name in PACK_ALIASES.get(lang, (lang,)):
        try:
            return get_parser(name)
        except Exception as ex:  # noqa: BLE001
            last = ex
    raise last


def _via_languages(lang):
    from tree_sitter_languages import get_parser
    last = None
    for name in PACK_ALIASES.get(lang, (lang,)):
        try:
            return get_parser(name)
        except Exception as ex:  # noqa: BLE001
            last = ex
    raise last


def _via_pkg(lang):
    from tree_sitter import Language, Parser
    modname, fn = PKG_MODULE.get(lang, ("tree_sitter_" + lang, "language"))
    mod = importlib.import_module(modname)
    lang_obj = Language(getattr(mod, fn)())
    try:
        return Parser(lang_obj)
    except TypeError:          # 旧版 API
        p = Parser()
        p.set_language(lang_obj)
        return p


LOADERS = (_via_pack, _via_languages, _via_pkg)


def get_parser(lang):
    if lang in _PARSERS:
        return _PARSERS[lang]
    parser, errs = None, []
    for loader in LOADERS:
        try:
            parser = loader(lang)
        except Exception as ex:  # noqa: BLE001
            errs.append("%s: %s: %s" % (loader.__name__.lstrip("_"), type(ex).__name__, str(ex)[:160]))
            parser = None
        if parser is not None:
            break
    _PARSERS[lang] = parser
    _LOAD_ERRORS[lang] = errs
    return parser


def load_errors(lang):
    return list(_LOAD_ERRORS.get(lang, []))


# ------------------------------------------------------------------ 遍历
class _Budget(Exception):
    pass


def _row(p):
    return p[0] if not hasattr(p, "row") else p.row


def _sig_text(node, lines):
    """签名文本：从声明起始行到函数体开始之前（体从行首开始则不含该行），最多 6 行。"""
    sr = _row(node.start_point)
    body = node.child_by_field_name("body")
    last = sr
    if body is not None:
        br, bc = body.start_point[0], body.start_point[1]
        if br > sr:
            row_text = lines[br].encode("utf-8") if br < len(lines) else b""
            lead = len(row_text) - len(row_text.lstrip())
            last = br - 1 if bc <= lead else br
    last = max(sr, min(last, sr + 5, len(lines) - 1))
    return " ".join(" ".join(x.split()) for x in lines[sr:last + 1])[:120]


def _end_row(node):
    er, ec = node.end_point[0], node.end_point[1]
    sr = node.start_point[0]
    return er - 1 if (ec == 0 and er > sr) else er


def _has_func_value(node):
    t = node.type
    if t in ("lexical_declaration", "variable_declaration"):
        for ch in node.named_children:
            if ch.type == "variable_declarator":
                v = ch.child_by_field_name("value")
                if v is not None and v.type in FUNC_VALUES:
                    return True
        return False
    v = node.child_by_field_name("value")       # field_definition / public_field_definition
    return v is not None and v.type in FUNC_VALUES


def _unwrap(node, spec, depth=0):
    field = spec["wrappers"].get(node.type)
    inner = node.child_by_field_name(field) if field else None
    if inner is None:
        for ch in node.named_children:
            if ch.type in spec["decl"] or ch.type in spec["special"] or ch.type in spec["wrappers"]:
                inner = ch
                break
    if inner is not None and inner.type in spec["wrappers"] and depth < 3:
        return _unwrap(inner, spec, depth + 1)
    return inner


def collect(root, spec, lines, opts):
    out = []
    budget = [MAX_VISITS]
    max_level = int(opts.get("max_depth", 6))
    desc_fn = bool(opts.get("descend_functions", False))
    decl, wrappers, special = spec["decl"], spec["wrappers"], spec["special"]

    def handle(target, start_node, level, depth):
        t = target.type
        if t in special:
            if _has_func_value(target):
                out.append((_row(start_node.start_point), level, _sig_text(target, lines), _end_row(start_node)))
            elif depth < MAX_RECURSION:
                visit(target, level, depth + 1)
            return
        role = decl.get(t)
        if role is None:
            if depth < MAX_RECURSION:
                visit(target, level, depth + 1)
            return
        has_body = target.child_by_field_name("body") is not None
        if t in spec["need_body"] and not has_body:
            if depth < MAX_RECURSION:
                visit(target, level, depth + 1)
            return
        if t in spec["skip_bodiless"] and not has_body:
            return
        out.append((_row(start_node.start_point), level, _sig_text(target, lines), _end_row(start_node)))
        if level < max_level and depth < MAX_RECURSION and (role == C or (role == F and desc_fn)):
            visit(target, level + 1, depth + 1)

    def visit(node, level, depth):
        for ch in node.children:
            if not ch.is_named:
                continue
            budget[0] -= 1
            if budget[0] <= 0:
                raise _Budget()
            t = ch.type
            if t in wrappers:
                inner = _unwrap(ch, spec)
                if inner is not None:
                    handle(inner, ch, level, depth)
                    continue
            elif t in decl or t in special:
                handle(ch, ch, level, depth)
                continue
            if depth < MAX_RECURSION:
                visit(ch, level, depth + 1)

    try:
        visit(root, 0, 0)
    except _Budget:
        return None
    return out


def declarations(text, ext, opts):
    lang = EXT_LANG.get(ext)
    spec = SPECS.get(lang) if lang else None
    if spec is None:
        return None
    parser = get_parser(lang)
    if parser is None:
        return None
    tree = parser.parse(text.encode("utf-8"))
    root = tree.root_node
    decls = collect(root, spec, text.split("\n"), opts)
    if decls is None:
        return None
    if not decls and getattr(root, "has_error", False):
        return None
    return decls


# ------------------------------------------------------------------ 自检样例（doctor 使用）
# 每项：(代码, 必须出现的子串, 不得出现的子串, {子串: 期望层级})
SELFTEST = {
    "python": ('''import os

@decorator(
    arg=1,
)
def decorated(a, b):
    """def fake_in_docstring():"""
    def inner_closure():
        return 1
    return a

class Service:
    def method_a(self):
        pass
''', ["decorated", "Service", "method_a"], ["fake_in_docstring", "inner_closure"], {"method_a": 1, "Service": 0}),
    "javascript": ('''export function exported(a, b) { return a; }
const arrow = async (req, res) => { return 1; };
class Store {
  action(state) { state.x = 1; }
}
module.exports = { helper() { return 3; } };
''', ["exported", "arrow", "Store", "action", "helper"], [], {"action": 1, "Store": 0}),
    "typescript": ('''export interface User { id: number }
export type Id = string;
enum Color { Red }
export abstract class Base {
  protected go(x: number): number { return x; }
}
''', ["User", "Id", "Color", "Base", "go"], [], {"go": 1}),
    "tsx": ('''export const App = () => <div>hi</div>;
export function Page() { return <App />; }
''', ["App", "Page"], [], {}),
    "go": ('''package main

type Server struct{ a int }

type Handler interface{ Run() }

func (s *Server) Run(ctx int) error {
	return nil
}

func main() {}
''', ["Server", "Handler", "Run", "main"], [], {}),
    "rust": ('''pub struct Worker { id: u32 }

impl Worker {
    pub fn run(&self) -> u32 { 1 }
}

pub trait Job { fn go(&self); }

mod inner { pub fn helper() {} }
''', ["Worker", "impl Worker", "run", "Job", "inner", "helper"], [], {"run": 1, "helper": 1}),
    "java": ('''package a;
public class Svc {
    @Override
    public int run(int a) { return a; }
    Svc() {}
    interface Inner { void x(); }
}
''', ["Svc", "run", "Inner"], [], {"run": 1}),
    "c": ('''#include <stdio.h>
struct point { int x; int y; };
typedef struct { int a; } pair_t;
static int calc(int a, int b)
{
    return a + b;
}
#define SQUARE(x) ((x)*(x))
''', ["point", "pair_t", "calc", "SQUARE"], [], {}),
    "cpp": ('''namespace ns {
class Foo {
 public:
  int bar(int a) { return a; }
};
}
template <typename T> T id(T v) { return v; }
int Foo::baz() { return 1; }
''', ["ns", "Foo", "bar", "id", "baz"], [], {"bar": 2}),
    "c_sharp": ('''namespace App {
  public class Svc {
    public int Run(int a) { return a; }
  }
}
''', ["Svc", "Run"], [], {"Run": 2}),
    "ruby": ('''class Svc
  def run(a)
    a
  end
end
''', ["Svc", "run"], [], {"run": 1}),
}
SAMPLE_EXT = {"python": ".py", "javascript": ".js", "typescript": ".ts", "tsx": ".tsx", "go": ".go", "rust": ".rs",
              "java": ".java", "c": ".c", "cpp": ".cpp", "c_sharp": ".cs", "ruby": ".rb"}
