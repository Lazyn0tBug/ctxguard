"""注册表自检：单一来源的东西必须有测试盯着，否则改了没人知道。

tools.TOOLS 是五个工具的唯一声明处（CLI 子命令与 MCP 工具列表都从它生成），
所以它本身的漂移要在这里被拦住：

1. 注册表声明的参数名 vs 实现函数的签名——名字对不上会 TypeError，
   或者更隐蔽：实现里多出的参数永远走默认值
2. 必填参数缺失时必须报错，不能静默补默认值
3. 生成的 MCP schema 形状（required 列表、enum、类型）
"""
import inspect
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import tools  # noqa: E402


def test_registry_lists_every_tool():
    assert {t.name for t in tools.TOOLS} == {"tree", "outline", "read", "search", "symbols"}
    assert set(tools.BY_NAME) == {t.name for t in tools.TOOLS}
    for t in tools.TOOLS:
        assert t.summary, "%s 缺少说明（会出现在 MCP 的 description 里）" % t.name


@pytest.mark.parametrize("tool", tools.TOOLS, ids=lambda t: t.name)
def test_declared_args_match_implementation(tool):
    """实现多出的参数只能是刻意不暴露的内部参数。"""
    params = set(inspect.signature(tool.fn).parameters) - {"root"}
    declared = {a.name for a in tool.args}
    assert params - declared == tools.INTERNAL_PARAMS.get(tool.name, set()), (
        "%s 的实现参数 %s 没在注册表里声明" % (tool.name, sorted(params - declared)))
    assert declared - params == set(), (
        "%s 的注册表声明了实现没有的参数 %s" % (tool.name, sorted(declared - params)))


def stub_tool(*args):
    """造一个只记录 kwargs 的假工具——单元测试不该碰文件系统。"""
    seen = {}

    def fn(root, **kwargs):
        seen.update(kwargs)
        seen["root"] = root
        return "ok"

    return tools.Tool("stub", "桩工具", fn, args), seen


def test_required_args_are_enforced():
    """必填参数缺失（或为 null）必须报错，不能补默认值蒙混过去。"""
    tool, seen = stub_tool(tools.Arg("path", required=True), tools.Arg("limit", "int", "limit", 40))
    with pytest.raises(ValueError):
        tools.call_tool(tool, "/tmp", {})
    with pytest.raises(ValueError):
        tools.call_tool(tool, "/tmp", {"path": None})
    assert tools.call_tool(tool, "/tmp", {"path": "x"}) == "ok"
    assert seen == {"root": "/tmp", "path": "x", "limit": 40}, "缺失的可选参数应走默认值"


@pytest.mark.parametrize("tool", tools.TOOLS, ids=lambda t: t.name)
def test_declared_required_args_are_enforced_for_real_tools(tool):
    """真实工具的必填参数同样必须被强制（用桩跑，避免碰文件系统）。"""
    required = [a.name for a in tool.args if a.required]
    if not required:
        pytest.skip("%s 没有必填参数" % tool.name)
    stub, _ = stub_tool(*tool.args)
    for name in required:
        # 每次只把一个必填参数置空，其余照常给
        values = {a.name: (None if a.name == name else "占位") for a in tool.args}
        with pytest.raises(ValueError):
            tools.call_tool(stub, "/tmp", values)


def test_schema_shape():
    """MCP 的 inputSchema 由注册表生成，形状必须对。"""
    read = tools.tool_schema(tools.BY_NAME["read"])
    assert read["type"] == "object"
    assert read["required"] == ["path"]
    assert read["properties"]["path"]["type"] == "string"
    assert read["properties"]["start"]["type"] == "integer"
    assert read["properties"]["end"]["type"] == "integer"
    assert read["properties"]["path"]["description"], "参数说明是模型可见的，别漏"

    search = tools.tool_schema(tools.BY_NAME["search"])
    assert search["required"] == ["pattern"]
    assert search["properties"]["mode"]["enum"] == ["auto", "files", "lines"]
    assert search["properties"]["limit"]["type"] == "integer"

    symbols = tools.tool_schema(tools.BY_NAME["symbols"])
    assert "required" not in symbols, "symbols 的参数都可省，不该有 required"
    assert set(symbols["properties"]) == {"query", "path", "limit"}


def test_cli_and_mcp_expose_the_same_tools():
    """CLI 子命令与 MCP 工具列表同源（CLI 侧由 index.py 从注册表生成）。"""
    assert {t.name for t in tools.TOOLS} == set(tools.BY_NAME)
