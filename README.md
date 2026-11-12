# ctxguard：索引优先的读取约束（语言无关，零依赖）

目标：**让任何一次工具调用都不可能把大块内容放进 Claude Code 的上下文**，而不是事后截断。
做法是取消“读任意内容”的能力，只保留“按索引下钻”的能力：`目录 → 段 → 行`。

需要 Python 3.13+（标准库），不需要向量库、不需要联网、不需要解析器。

## 安装

```bash
python3 install.py /path/to/your/project      # 项目级（推荐）
python3 install.py --user                      # 仅 hook，对所有项目生效
python3 install.py --uninstall /path/to/project
python3 install.py /path/to/project --wrap <mcp名>   # 给第三方 MCP 套上“大输出转存”代理
```

**解释器要求**：装进目标项目的代码需要 Python 3.13+（下限写在 `pyproject.toml` 的
`requires-python` 里，是唯一事实来源）。安装器会自己探测一个达标解释器（`python3.13`、
`python3.14`、`python3.15`、PATH 上的 `python3`……）并把它的名字写进 hook 命令——
不能假设目标机器的 `python3` 够新，很多 macOS 自带的仍是 3.9。找不到达标解释器时安装器
直接报错退出，不会留下一个跑不起来的 hook。

安装器自身刻意保持老 Python 可运行：你可以用 `python3.9 install.py ...` 装出一套用
3.13 跑的工具。

安装后重启 Claude Code，用 `/hooks` 和 `/mcp` 确认。安装器是幂等的，会合并而不是覆盖你已有的
`.claude/settings.json`、`.mcp.json`、`CLAUDE.md`。

## 它怎么约束（三道闸）

| 闸 | 机制 | 管什么 |
|---|---|---|
| 1. 形态 | 索引器把任何文本文件切成“带标题和一句摘要的段”（代码按声明/缩进，文档按标题，其余按空行窗口），存进 `.ctxindex/index.db` | 把“全文”变成“目录” |
| 2. 入口 | PreToolUse hook 拦截 `Read`、`Grep(content)`、`Bash(cat/head/sed/git grep/rg/git diff/find…)`，超限**拒绝**并在反馈里直接附上目录 | 模型被迫改走下钻，不会拿到半截内容后反复重试 |
| 3. 载荷 | MCP 代理把第三方工具的巨大返回转存成文件，只返回目录 | 解决“47 万字符 + 请逐块读完” |

另有：`ctxguard` MCP 服务（`tree / outline / read / search / symbols`，没有“读全文”入口）、
SessionStart 注入目录概览、主线程**累计读取预算**（防止“分块读完整份”）、CLAUDE.md 规则（约束回显）。

## 语言无关是怎么做到的

不解析语法，用通用启发式：声明关键字（`def/function/class/fn/func/impl/struct/…`）、箭头函数、C 系
“签名行 + 下一行花括号”、`name() {` 的 shell 函数、SQL `CREATE`、YAML 顶层键、TOML/INI 节、Markdown/RST/AsciiDoc/Org 标题；
再按缩进层级递归细分，直到每段不超过约 120 行 / 6000 字符。识别不出结构的文件（日志、纯文本）按空行切窗口。
压缩成一行的 JSON/JS（超长行）会生成格式化后的可读视图，再对视图建目录。

已验证的样例：Python、JS、C、Go、Rust、Java、SQL、Shell、YAML、中文 Markdown、RST、AsciiDoc、Org、压缩 JSON、纯文本；
以及真实的 npm 源码树（JS + Markdown + HTML + man）。

## 可调参数（项目根 `.ctxguard.json`，均可省略）

```json
{ "max_read_tok": 4000, "session_budget": 100000, "allow": ["docs/api/*.md"], "grep_content_max": 60, "bash_guard": true }
```

- `max_read_tok`：单次读取上限（估算 tokens），超过即拒绝
- `session_budget`：主线程累计读取预算，0 为关闭；`/clear`、`/compact` 后自动重置
- `allow`：允许整份读取的路径（glob）
- 环境变量：`CTXGUARD_ROOT`、`CTXGUARD_PROXY_TOK`（代理转存阈值，默认 6000）、`CTXGUARD_BASH=off`

配置项的**类型写错不会让 hook 失效**：非法值退回默认并给出警告（stderr，且 SessionStart
会回显）。比如 `"max_read_tok": "abc"` 只会退回 4000 并提示，不会让整个读取约束静默消失。

## 命令行（人工检查索引质量）

```bash
python3 .claude/ctxguard/index.py tree
python3 .claude/ctxguard/index.py outline path/to/file
python3 .claude/ctxguard/index.py search 'pattern' --path src
```

## 已知边界（请知悉）

1. **Bash 是万能后门**：`python -c "print(open(f).read())"`、`awk`、`xargs cat` 之类的写法 hook 无法穷尽。
   这里的 Bash 拦截是尽力而为；要严格，把 Bash 改为 allowlist，或用沙箱隔离。
2. **回显是软约束**：模型在答复里贴整份内容发生在生成阶段，hook 管不到，只能靠 CLAUDE.md 规则和“大内容进不了上下文”间接减少。
3. **token 为估算**（ASCII 约 3.6 字符/token，中日韩约 1 字符/token），与真实计数有偏差，阈值留有余量。
4. hook 内部异常一律放行（fail-open），避免 hook 自身故障卡死会话。配置项类型错误是例外：
   已单独校验并退回默认值，不会走到这条兜底。
5. 子 agent 的累计读取不计入主线程预算，依赖 hook 输入里的 `agent_id` 字段（在 Claude Code 2.1.322 上未专门验证）。
6. 单次拒绝本身也会占用少量上下文（目录最多 60 行），这是有意的取舍：用一次小的目录换掉一次大的读取。

## 可选的声明提取后端（pyast / tree-sitter）

默认关闭，行为不变。开启后，代码文件的切分点由真实语法决定，不再靠行首文本猜测（修复 docstring/字符串里的伪声明、多行装饰器、闭包等问题）。

```bash
python3 install.py <项目> --backend pyast          # 零依赖，仅 Python
python3 install.py <项目> --backend auto           # treesitter（装了才生效）优先，其次 pyast，再回退启发式
pip install tree-sitter tree-sitter-language-pack   # tree-sitter 后端的依赖（在目标机器上）
python3 .claude/ctxguard/index.py doctor            # 逐语言自检
python3 .claude/ctxguard/index.py compare <文件>    # 启发式 vs 后端 的差异对照
```

也可写在 `.ctxguard.json`：`{"backends": ["treesitter", "pyast"], "backend_options": {"descend_functions": false}}`。
项目级配置只接受内置后端名；自定义插件（`plugin:/绝对路径.py`）只接受 `~/.ctxguard.json` 或环境变量 `CTXGUARD_BACKENDS`。
后端、版本、依赖版本或选项变化时，旧索引自动作废并重建。完整设计、接口契约与调试指南见设计文档。

**注意**：tree-sitter 后端只用假节点测过，真实语法包的节点名需要在目标机器上用 `doctor` 核对（只改 `backend_ts.py` 的 `SPECS` 表）。

## 测试

两支脚本测试（83 项端到端 + 54 项后端）也可以直接跑：

```bash
python3 tests/gen_sample.py /tmp/sample && (cd /tmp/sample && git add -A)
python3 tests/test_all.py "$(pwd)" /tmp/sample      # 83 项：索引、hook、预算、MCP、代理
python3 tests/test_backends.py "$(pwd)"             # 54 项 + 装了 tree-sitter 后追加的真实语法自检
```

## 开发（uv + ruff + ty）

工程配置在 `pyproject.toml`：`uv` 管依赖与解释器，`ruff` 管风格与常见缺陷，`ty` 管类型。
运行时依然零依赖——`pyproject.toml` 只是开发期的工程描述，ctxguard 本身靠 `install.py`
复制散装 `.py` 投放，不走打包。

```bash
uv sync                          # 建 .venv（Python 3.13），装 ruff / ty / pytest
uv run ruff check .              # 静态检查：E,F,W,I,N,UP,B,SIM
uv run ty check .                # 类型检查
uv run pytest                    # 自动造样例仓库并跑完 137 项检查
uv run pytest -q                 # 同上，静默模式
uv sync --group treesitter       # 额外装 tree-sitter，让后端的真实语法自检也跑起来
```

两条规则按项目风格放宽，改动前先看 `pyproject.toml` 里的注释：

- `E501`（行宽）：中文注释按字符计宽天然超 100，强行折行会切碎语义。
- `UP031`（`%` 格式化）：项目一致使用，改 f-string 收益低、churn 大。

