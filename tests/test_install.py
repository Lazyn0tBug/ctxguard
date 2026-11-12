"""安装 / 卸载的往返测试。

卸载的语义是"**回到安装之前**"：用户自己的 hooks、MCP 服务、CLAUDE.md 内容、
.gitignore 规则都必须原样保留，只撤掉 ctxguard 加的那些。

这条路径以前完全没有测试覆盖过——包括"会不会误删用户自己的配置"这种最该问的问题。
"""
import json
import os
import subprocess
import sys

import pytest

SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INSTALL = os.path.join(SRC, "install.py")
PY = sys.executable

USER_SETTINGS = {
    "hooks": {
        "SessionStart": [{"hooks": [{"type": "command", "command": "echo 用户自己的 hook"}]}],
    },
    "permissions": {"deny": ["Read(./secrets/*)"]},
}
USER_MCP = {
    "mcpServers": {"别人的服务": {"command": "node", "args": ["server.js"]}},
}
USER_CLAUDE_MD = "# 我的项目\n\n这是我自己写的内容。\n"
USER_GITIGNORE = "*.log\n"


@pytest.fixture
def project(tmp_path):
    """一个"用户自己的项目"：settings / .mcp.json / CLAUDE.md / .gitignore 都已有内容。"""
    root = str(tmp_path)
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "settings.json").write_text(
        json.dumps(USER_SETTINGS, ensure_ascii=False, indent=2), encoding="utf-8")
    (tmp_path / ".mcp.json").write_text(
        json.dumps(USER_MCP, ensure_ascii=False, indent=2), encoding="utf-8")
    (tmp_path / "CLAUDE.md").write_text(USER_CLAUDE_MD, encoding="utf-8")
    (tmp_path / ".gitignore").write_text(USER_GITIGNORE, encoding="utf-8")
    (tmp_path / "main.py").write_text("print('hi')\n", encoding="utf-8")
    return root


def run_install(project, *args):
    p = subprocess.run([PY, INSTALL, project, *args], capture_output=True, text=True)
    assert p.returncode == 0, p.stdout + p.stderr
    return p.stdout


def read_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def test_install_then_uninstall_restores_the_project(project):
    """装完再卸完，项目回到安装前的状态。"""
    run_install(project)

    # ---- 装完：ctxguard 的痕迹都在
    settings = read_json(os.path.join(project, ".claude", "settings.json"))
    assert "PreToolUse" in settings["hooks"], "装完应有 PreToolUse hook"
    mcp = read_json(os.path.join(project, ".mcp.json"))
    assert "ctxguard" in mcp["mcpServers"], "装完应有 ctxguard MCP 条目"
    with open(os.path.join(project, "CLAUDE.md"), encoding="utf-8") as f:
        assert "ctxguard:begin" in f.read(), "装完应写入读取纪律"
    with open(os.path.join(project, ".gitignore"), encoding="utf-8") as f:
        assert ".ctxindex/" in f.read(), "装完应忽略索引目录"
    assert os.path.isdir(os.path.join(project, ".claude", "ctxguard")), "装完应有工具目录"
    assert os.path.isdir(os.path.join(project, ".ctxindex")), "装完应建索引"

    # ---- 卸完：ctxguard 的痕迹全没，用户自己的全在
    run_install(project, "--uninstall")

    settings = read_json(os.path.join(project, ".claude", "settings.json"))
    assert "PreToolUse" not in settings.get("hooks", {}), "卸完应撤掉 PreToolUse hook"
    assert settings["hooks"]["SessionStart"], "用户的 SessionStart hook 必须还在"
    assert settings["permissions"]["deny"] == ["Read(./secrets/*)"], "用户的 deny 规则必须还在"
    assert not any("ctxindex" in d for d in settings["permissions"]["deny"]), "应撤掉 ctxguard 的 deny 规则"

    mcp = read_json(os.path.join(project, ".mcp.json"))
    assert "ctxguard" not in mcp.get("mcpServers", {}), "卸完应撤掉 ctxguard MCP 条目"
    assert mcp["mcpServers"]["别人的服务"] == USER_MCP["mcpServers"]["别人的服务"], "用户的 MCP 服务必须原样"

    with open(os.path.join(project, "CLAUDE.md"), encoding="utf-8") as f:
        md = f.read()
    assert "ctxguard:begin" not in md, "卸完应撤掉读取纪律"
    assert "这是我自己写的内容。" in md, "用户的 CLAUDE.md 内容必须还在"

    with open(os.path.join(project, ".gitignore"), encoding="utf-8") as f:
        assert f.read() == USER_GITIGNORE, "卸完 .gitignore 应回到原样"

    assert not os.path.exists(os.path.join(project, ".claude", "ctxguard")), "卸完应删掉工具目录"
    assert not os.path.exists(os.path.join(project, ".ctxindex")), "卸完应删掉索引目录"


def test_uninstall_removes_the_gitignore_it_created(project):
    """项目原本没有 .gitignore 时，安装器建的应该在卸载后被删掉（不留空文件）。"""
    os.remove(os.path.join(project, ".gitignore"))
    run_install(project)
    assert os.path.exists(os.path.join(project, ".gitignore"))
    run_install(project, "--uninstall")
    assert not os.path.exists(os.path.join(project, ".gitignore")), "安装器建的空 .gitignore 应被删掉"


def test_uninstall_restores_a_proxied_mcp_service(project):
    """被 --wrap 套过代理的第三方服务，卸载后要还原成原来的 command/args。"""
    mcp = read_json(os.path.join(project, ".mcp.json"))
    mcp["mcpServers"]["别人的服务"]["args"] = ["server.js", "--flag"]
    with open(os.path.join(project, ".mcp.json"), "w", encoding="utf-8") as f:
        json.dump(mcp, f, ensure_ascii=False, indent=2)

    run_install(project, "--wrap", "别人的服务")
    wrapped = read_json(os.path.join(project, ".mcp.json"))
    assert wrapped["mcpServers"]["别人的服务"]["command"] != "node", "套代理后 command 应指向代理"
    assert "--" in wrapped["mcpServers"]["别人的服务"]["args"], "套代理后 args 应带 --"

    run_install(project, "--uninstall")
    restored = read_json(os.path.join(project, ".mcp.json"))
    assert restored["mcpServers"]["别人的服务"]["command"] == "node", "卸完应还原 command"
    assert restored["mcpServers"]["别人的服务"]["args"] == ["server.js", "--flag"], "卸完应还原 args"


def test_install_is_idempotent(project):
    """重复安装不应把用户的配置搞乱（合并而不是覆盖）。"""
    run_install(project)
    first = read_json(os.path.join(project, ".claude", "settings.json"))
    run_install(project)
    second = read_json(os.path.join(project, ".claude", "settings.json"))
    assert len(second["hooks"]["PreToolUse"]) == len(first["hooks"]["PreToolUse"]), "重复安装不应叠加 hook"
    assert second["permissions"]["deny"] == first["permissions"]["deny"], "重复安装不应叠加 deny 规则"
