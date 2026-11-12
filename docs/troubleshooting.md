# ctxguard 排障指南

按**症状**组织：先确认哪道闸活着，再对症处理。命令与路径都按项目级安装（`.claude/ctxguard/`）写；
用户级安装（`~/.claude/ctxguard/`）把路径换掉即可，其余相同。

## 0. 先做这三件事（30 秒定位）

```bash
cd /path/to/your/project

# 1. 工具活着吗？SessionStart 会打印索引统计与目录概览
echo '{"cwd":"'$PWD'"}' | python3 .claude/ctxguard/hook_session.py | head -3

# 2. 索引活着吗？
python3 .claude/ctxguard/index.py tree

# 3. hook 用的解释器够新吗？
grep '"command"' .claude/settings.json    # 先看清楚用的是哪个解释器
python3.13 --version      # 再问**那个**解释器的版本，必须 >= 3.13
```

- 第 1 步没输出 → hook 脚本本身起不来（解释器版本不对、或文件没复制全），看 §2.1
- 第 1 步有输出、第 2 步没输出 → 索引没建起来，看 §2.4
- 第 3 步版本 < 3.13 → hook 会在 import 期就崩，而 hook 的兜底是**放行**（fail-open），
  于是表现为"约束完全不生效"。用 `python3.13 install.py <项目>` 重装即可
- ⚠️ 要问的是 **hook 命令里那个解释器**的版本，不是 PATH 上 `python3` 的版本——两者常常
  不是同一个（PATH 上的 `python3` 可能还是 3.9，而 hook 用的是 `python3.13`）

## 1. 三道闸各自的验证方法

| 闸 | 怎么确认它在工作 | 期望 |
|---|---|---|
| 形态（索引） | `python3 .claude/ctxguard/index.py outline <文件>` | 输出分段目录：起止行、tokens、标题、摘要 |
| 入口（hook） | 见下面的手工调用 | exit=2 且 stderr 带着目录 |
| 载荷（代理） | 套上后调一次大返回的 MCP 工具 | 返回里出现"已转存为 …" |

**手工调用 hook**（最直接的诊断手段，能区分"没拦"和"脚本坏了"）：

```bash
echo '{"tool_name":"Bash","tool_input":{"command":"cat big.txt"},"cwd":"'$PWD'"}' \
  | python3 .claude/ctxguard/hook_pre.py; echo "exit=$?"
```

| exit | 含义 |
|---|---|
| 2 | 正常拒绝 ✓ |
| 0 | 放行——要么确实不该拦（见 §2.2），要么 hook 内部抛异常被 fail-open 吞了 |
| 其他 | 脚本本身坏了（解释器不对、文件缺失）；把 stderr 全看一遍 |

## 2. 症状对照

### 2.1 hook 完全不生效（什么都不拦）

按可能性排序：

1. **解释器版本不对**（最常见）。`grep '"command"' .claude/settings.json` 看实际用的命令，
   `python3 --version` 必须 ≥ 3.13。macOS 自带的 `/usr/bin/python3` 常常是 3.9。
   → `python3.13 install.py <项目>` 重装（安装器会自己探测达标解释器并写进配置）
2. **Claude Code 没重载配置**。改完 `settings.json` 要重启会话；用 `/hooks` 确认 PreToolUse 已加载
3. **hook 内部异常被 fail-open 吞了**。hook 的设计是"任何内部异常一律放行"（免得 hook 自身故障
   卡死会话），代价就是**坏了的时候表现为"什么都不拦"而不是报错**。用 §1 的手工调用看 exit 码：
   若是 0 且该拦，说明是内部异常——最常见的原因是配置项类型不对（见 §2.6）
4. **装的是用户级而不是项目级**（或反之）。用户级安装只写 `~/.claude/settings.json`，
   项目级的 MCP 与 CLAUDE.md 规则不生效

**装的时候就失败**（还没进会话）：

```
错误：安装器需要 Python 3.11+（缺 tomllib）。当前 3.9.6；请用 python3.13 运行本安装器。
```

安装器用 `tomllib` 读 `pyproject.toml`，所以它自己最低要 3.11。3.11 / 3.12 可以跑
（它会自己去 PATH 上探测一个 3.13+ 的解释器来装工具），3.9 不行 → 换 `python3.13 install.py ...`。

### 2.2 大文件没有被拦

1. **路径在 `allow` 里**。查 `.ctxguard.json` 的 `allow`（glob，相对项目根），命中就整份放行
2. **Bash 拦截被关了**：`.ctxguard.json` 的 `"bash_guard": false` 或环境变量 `CTXGUARD_BASH=off`
3. **阈值太高**：`max_read_tok` 默认 4000（估算 tokens）。文件比这小就放行
4. **文件是二进制/图片**：`.png` `.jpg` `.pdf` 等一律放行（它们不进上下文）。PDF 例外：
   不给 `pages` 参数且超过 2MB 会被拦
5. **Bash 是万能后门**：`python -c "print(open(f).read())"`、`awk`、`xargs cat` 之类 hook 无法穷尽。
   这是**已知边界**，不是 bug；要严格得把 Bash 改成 allowlist 或用沙箱隔离

### 2.3 目录（outline）切得不对

先看是哪一类问题，用两个命令定位：

```bash
python3 .claude/ctxguard/index.py doctor          # 后端自检
python3 .claude/ctxguard/index.py compare <文件>    # 启发式 vs 后端，并排对照
```

**`compare` 的输出怎么读**：

```
启发式声明 3 个；后端声明 3 个。

只在启发式里（可能是误报：字符串/注释里的伪声明、闭包等）: 0
只在后端里（启发式漏掉的）: 0

[启发式] 目录：1 段，最大 31 tok
  1-9 | ~31tok | import os

[后端] 目录：1 段，最大 31 tok
  1-9 | ~31tok | import os
```

- "**只在启发式里**"：启发式把字符串里的伪声明、闭包当成了真声明 → 目录切得太碎
- "**只在后端里**"：启发式漏了真声明 → 目录太粗
- 下面两段目录是同一文件在两套切分下的结果；**逐段比行号**就能看出分歧在哪一层

**常见成因**：

| 现象 | 原因 | 处理 |
|---|---|---|
| 目录按空行窗口切的，没有标题 | 文件没有可识别的结构（纯日志、说明性文本） | 这是设计如此；换 `CTXGUARD_CHUNK_LINES` 只能调窗口大小，不能造出标题 |
| 段标题是文件第一行（如 `import os`） | 短文件被合并成一段，标题取首行 | 正常；文件长一些才会按声明/标题分开 |
| 压缩 JSON/JS 切不出段 | 整文件一行，行内无法切 | 自动生成可读视图 `.ctxindex/views/<路径>.view`，对它再 outline |
| 真实语法切点不对（代码） | 启发式按行首文本猜 | 装后端：`python3 install.py <项目> --backend pyast`（仅 Python，零依赖）或 `--backend auto`（含 tree-sitter） |

**`doctor` 的输出怎么读**：

```
== ctxguard doctor ==
项目根: /path/to/project
Python: 3.15.0
生效的后端（按优先级）: (无，仅内置启发式)
索引签名: v3|120|6000|none

[pyast] 已加载 v1 (py3.15)
   ✓ python     3 个声明, 0.2ms

[treesitter] 已加载 v1 (tree-sitter=-,...)
   - python     解析器不可用:
       via_pack: ModuleNotFoundError: No module named 'tree_sitter_language_pack'
```

- `✓ 语言 N 个声明`：该语言自检通过
- `解析器不可用`：tree-sitter 依赖没装（`pip install tree-sitter tree-sitter-language-pack`），
  不影响其它后端
- `索引签名`：`v3|120|6000|none` = 版本|段行数上限|段字符上限|后端签名。
  **后端或这些参数一变，旧索引自动作废重建**——所以刚改完配置目录没变，多半是索引还没重建

### 2.4 索引没建起来 / 是旧的

```bash
python3 .claude/ctxguard/index.py build --force   # 强制重建
python3 .claude/ctxguard/index.py build --quiet    # 增量续建（大仓库一次建不完，会跨会话续）
```

- 索引在 `<项目>/.ctxindex/index.db`
- 大仓库（几万文件）一次建不完：SessionStart 的预算是 25 秒，会分多次会话续建，
  中途是正常状态
- 改了分块参数（`CTXGUARD_CHUNK_LINES` / `CTXGUARD_CHUNK_CHARS`）或换了后端 → 签名变化 →
  旧索引作废，下次自动重建
- 索引目录整个被 `.gitignore` 忽略（`.ctxindex/.gitignore` 里是 `*`），不会污染仓库

### 2.5 search 结果不全

输出里会明确写"**超时未搜完**"：

```
321 处匹配，分布在 14402 个文件（范围 21667 个文件，超时未搜完）
```

search 有 15 秒的硬预算，几万文件的仓库会扫不完。缩小范围即可：

```bash
python3 .claude/ctxguard/index.py search 'pattern' --path src   # 缩到子目录
python3 .claude/ctxguard/index.py search 'pattern' --glob '*.py' # 缩到文件类型
```

`--mode files`（默认）先给"文件→段"的分布，`--mode lines` 才给具体行（受 `--limit` 限制，
上限 100）。

### 2.6 配置不生效

**优先级**：环境变量 > 项目 `.ctxguard.json` > 用户 `~/.ctxguard.json` > 默认值。

两个例外：

- **`backends`**：项目级配置只接受内置后端名（`pyast` / `treesitter`）。
  任意模块名或 `plugin:/绝对路径.py` 只接受用户级配置或环境变量 `CTXGUARD_BACKENDS`
  ——项目文件可能来自不可信仓库，不能让它决定加载哪段代码
- **类型写错**：非法值**退回默认并警告**，不会让 hook 失效。警告出现在 stderr，
  SessionStart 也会回显到 stdout：

  ```
  [ctxguard] 配置项 项目 max_read_tok 期望整数，实际 'abc' —— 已退回默认值
  ```

  看不到警告？检查 SessionStart 的输出（`/hooks` 确认它加载了）

### 2.7 会话预算被吃光

```
[ctxguard] 拒绝：本会话主线程累计已读取约 100312tok，预算 100000tok 用尽。
```

- 预算是**主线程累计**的读取量，默认 100000 估算 tokens；`/clear`、`/compact` 后自动重置
- 计数文件在 `.ctxindex/session-<会话 id>.json`，删掉它等于手动重置
- **子 agent 的读取不计入**（那里正是"读很多"的指定场所），依赖 hook 输入里的 `agent_id` 字段
- 调大：`.ctxguard.json` 的 `session_budget`，或设 `0` 关闭

### 2.8 卸载

```bash
python3 install.py --uninstall <项目>       # 撤掉项目级安装
python3 install.py --user --uninstall       # 撤掉用户级安装（只影响 ~/.claude）
```

卸载的语义是"**回到安装之前**"——你自己的东西一律不动：

| 会被撤掉 | 会原样保留 |
|---|---|
| `.claude/settings.json` 里的 ctxguard hook 与 deny 规则 | 你自己的 hook 与 deny 规则 |
| `.mcp.json` 里的 ctxguard 条目 | 你自己的 MCP 服务 |
| 被 `--wrap` 套过代理的服务的代理层（还原成原来的 command/args） | 服务的原始配置 |
| `CLAUDE.md` 里的"读取纪律"段 | 你自己写的其它内容 |
| `.gitignore` 里的 `.ctxindex/`、`.claude/ctxguard/__pycache__/` | 你自己的忽略规则 |
| `.claude/ctxguard/` 与 `.ctxindex/` 目录 | 别的全部 |

安装器建的 `.gitignore`（项目原本没有、装完只有那两条）在卸载后会被删掉，不留空文件。

### 2.9 MCP 服务连不上

```bash
python3 .claude/ctxguard/server.py          # 手动起，应该安静地等 stdin
```

- `.mcp.json` 里应该有 `ctxguard` 条目，command 指向 `python3 .claude/ctxguard/server.py`
- Claude Code 里用 `/mcp` 看连接状态；项目级 `.mcp.json` 首次需要批准
- 服务端只在 stdout 写 JSON-RPC，**任何多余输出都会破坏协议**——所以它的输出要全部走
  `send()`，不要 print

### 2.10 代理没套上 / 没转存

```bash
python3 install.py <项目> --wrap <.mcp.json 里的服务名>
```

- 名字必须与 `.mcp.json` 的 `mcpServers` 键完全一致，否则报"里没有名为 X 的 mcpServers 条目"
- 重复套会提示"已经套过代理，无需重复"
- 转存阈值是 6000 估算 tokens（`CTXGUARD_PROXY_TOK` 可调）
- 转存文件在 `.ctxindex/stash/`，返回给模型的只是"已转存 + 目录"，原文不进上下文

## 3. 命令速查

```bash
IDX=.claude/ctxguard/index.py

python3 $IDX tree [路径] [--depth N]        # 目录树（每项带文件数/tokens/段数）
python3 $IDX outline <文件>                 # 某文件的分段目录
python3 $IDX read <文件> [--start N] [--end N]
python3 $IDX search <pattern> [--path P] [--mode auto|files|lines] [--glob G] [--limit N]
python3 $IDX symbols [名字] [--path P] [--limit N]
python3 $IDX build [--force] [--quiet] [--time-limit SEC]
python3 $IDX doctor                         # 后端自检
python3 $IDX compare <文件> [--backend NAME] # 启发式 vs 后端对照
```

## 4. 调参对照

| 想改什么 | 改哪儿 |
|---|---|
| 单次读取上限 | `.ctxguard.json` 的 `max_read_tok`（默认 4000 估算 tokens）|
| 会话累计预算 | `session_budget`（默认 100000，0=关闭）|
| 允许整份读取某些文件 | `allow`（glob 数组，相对项目根）|
| Grep content 模式的 head_limit 上限 | `grep_content_max`（默认 60）|
| 关掉 Bash 拦截 | `bash_guard: false` 或 `CTXGUARD_BASH=off` |
| 段的大小 | 环境变量 `CTXGUARD_CHUNK_LINES`（默认 120）/ `CTXGUARD_CHUNK_CHARS`（默认 6000）|
| 代理转存阈值 | `CTXGUARD_PROXY_TOK`（默认 6000）|
| 索引位置 | `CTXGUARD_ROOT` |
| 声明提取后端 | `.ctxguard.json` 的 `backends`（`["pyast"]` / `["treesitter"]` / `["auto"]`）|

改分块参数或换后端会让索引签名变化 → **旧索引自动作废重建**。

## 5. 索引目录里有什么

```
<项目>/.ctxindex/
  index.db          # 索引本体（SQLite，WAL 模式）
  .gitignore        # 内容是 "*"，整个目录不进版本库
  views/            # 超长行文件（压缩 JSON/JS）的可读视图
  stash/            # 代理转存的大返回
  session-*.json    # 各会话的累计读取量
  backend.log       # 后端加载/运行错误
```

## 6. 这些是设计边界，不是 bug

1. **Bash 是万能后门**——hook 拦的是常见读法，穷举不了 `python -c` / `awk` / `xargs cat`
2. **回显是软约束**——模型在答复里贴整份内容发生在生成阶段，hook 管不到
3. **token 是估算**——ASCII 约 3.6 字符/token，中日韩约 1 字符/token，与真实计数有偏差
4. **hook 内部异常一律放行**（fail-open）——见 §2.1 第 3 条
5. **子 agent 不计入主线程预算**——依赖 hook 输入里的 `agent_id`
6. **拒绝本身也占上下文**——目录最多 60 行，是有意取舍
