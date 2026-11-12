# ctxguard 的渐进式 Rust 迁移设计

> 状态：设计稿，待评审。目标读者：实现者。
> 本文档描述"把 ctxguard 逐步迁到 Rust"的切分方式、接口契约、迁移顺序与验收标准。
> **行为不变是硬约束**：现有 144 项黑盒测试是迁移的验收标准，实现对它们负责。

## 0. 摘要

ctxguard 由 12 个平铺的 Python 模块组成（约 2,800 行），靠 `install.py` 复制散装 `.py` 投放，
由用户机器上的 `python3` 运行。2026-11 这一轮把它改造成 uv + ruff + ty 管理的工程之后，
暴露出一个根问题：

> **工具依赖一个用户机器上可能没有、或版本不对的 Python 解释器。**

围绕这个问题做出的所有工程措施——Python 版本下限、`install.py` 的解释器探测、
"找不到达标解释器就报错"、模块级标注在旧版本上求值崩溃导致 hook 静默放行——
全是治标。渐进迁移到 Rust 的意义不是性能，是**让这个根问题不再存在**。

**终局**：一个 Rust 二进制，承载全部功能（CLI / MCP / hook / 代理），
Python 依赖彻底消失。

**路径**：按职责切成 6 个切片，逐片迁移。每片是独立可执行文件，Python 侧探测到就用、
探测不到就走原实现兜底，直到该片的 Python 实现退役。

**性能是副产品，不是目标**：Rust 带来的"简单"（单文件、类型强制、无解释器）
才是迁移的主要收益。

## 1. 背景与证据

### 1.1 问题的来源

投放模型决定了运行时形态：`install.py` 把 11 个 `.py` 复制进 `<项目>/.claude/ctxguard/`，
hook 命令是 `python3 <dest>/hook_pre.py`，不建 venv、不装包。这保证了"复制即用"，
代价是运行时要一个解释器。

这个解释器带来一连串只能靠防御性代码兜住的问题：

| 问题 | 现状的应对 | 代价 |
|---|---|---|
| 用户 python3 版本过旧 | `requires-python = ">=3.13"` + `install.py` 探测解释器 | 探测逻辑 ~50 行；找不到就装不上 |
| 版本探测不到 | 报错退出，不留坏配置 | 装机失败是硬失败 |
| 导入期异常 | hook 的 `except Exception: return`（fail-open） | **约束静默失效**，用户毫无察觉 |
| 坏配置 | `int(cfg["max_read_tok"])` 抛 ValueError | 同上，fail-open 吞掉 |
| 类型错误 | 靠 ty + 标注（152 个函数目前只标了约 25 个） | 持续投入；漏标即漏检 |

fail-open 是这里最危险的默认：**工具"坏了"的表现是"什么都不拦"**，而不是报错。

### 1.2 量化基线（2026-11 实测，Apple Silicon / Python 3.13.18）

| 指标 | 数值 | 说明 |
|---|---|---|
| hook 启动 | **27.2 ms**（其中 25.4 ms 是 `import lib, tools`） | 每次工具调用都付 |
| 索引 433 文件（npm 树） | 0.1 s | 典型小仓 |
| 索引 53,726 文件（Python 标准库） | **45.8 s**（0.85 ms/文件） | SessionStart 的预算是 25 s，**建不完，靠多会话续建** |
| search（53k 文件仓） | **21.4 s**，而代码里的预算是 15 s | **超时截断**，模型拿到的是残缺结果 |
| ├ 扫描（读文件 + 正则） | 9.7 s | 语言问题，Rust 能解决 |
| └ 命中文件 → 查段 | 9.7 s | **实现方式问题**：每个命中文件重开一次 SQLite |
| 模块规模 | 12 个运行时模块 / 2,814 行 | lib 656、tools 622、backend_ts 403、hook_pre 357、install 315 |

### 1.3 一个与迁移无关、但立刻该修的问题

上表最后一行的 9.7 s：`_hit_chunks()` 对每个命中文件调用 `chunks_for()`，
而 `chunks_for()` 每次都 `open_db()` → `ensure_file()`（stat + 可能重建索引）→ 查询 → 关闭连接。
14,000 个命中文件 = 14,000 次开库。

**这是纯 Python 就能修的**（复用连接 + 批量查询），与是否迁 Rust 无关。
迁移前先修掉它，能立刻把 search 从 21.4 s 降到 ~12 s，也让"迁到 Rust 能省一半"这件事
不再被实现缺陷掩盖。列入 §11 的"迁移前可独立收获"。

## 2. 目标与非目标

### 目标

1. **单个二进制**，承载 CLI / MCP / hook / 代理；用户机器不需要 Python。
2. **类型由编译器保证**，不再依赖"记得写标注"来换取静态检查。
3. **hook / MCP / CLI 共享同一份实现**，消除"三条路径行为不一致"的可能。
4. **部署退化为"复制一个文件"**，`install.py` 不再需要探测解释器、管理版本下限。
5. 保持**全部现有行为**：144 项黑盒测试逐条通过。

### 非目标

1. **不追求极致性能**。Rust 会更快，但更快不是这次迁移的理由；不为性能牺牲行为一致性。
2. **不改产品形态**：仍然是"索引优先的读取约束"，三道闸的结构不变。
3. **不重写索引格式**：`.ctxindex/index.db` 的 SQLite schema 保持不变，它是 Python 与 Rust 之间的稳定接口。
4. **不追求一次到位**：任何时刻都存在一个可交付、可回退的状态。

## 3. 终局架构

```
                    ┌─────────────────────────────────────────┐
 Claude Code  ──────┤ ctxguard（单个 Rust 二进制）             │
   PreToolUse hook  │   guard   读 stdin → 决策 → exit 2      │
   SessionStart     │   mcp     JSON-RPC over stdio           │──▶ .ctxindex/index.db
   MCP 客户端       │   proxy   转发 + 转存                    │    （SQLite，schema 不变）
   你的 shell       │   tree outline read search symbols      │
                    │   build chunk scan doctor compare       │
                    └─────────────────────────────────────────┘
```

- **一个 crate**：`crates/ctxguard/`，`src/lib.rs` 是库（可测、可复用），`src/main.rs` 是 CLI。
- **Python 侧最终只剩**：可能连 `install.py` 都由 Rust 重写（`ctxguard install`）。
- **CLI 表面保持不变**：子命令名、参数名、输出格式与现在的 `index.py` 逐字一致，
  这样 144 项黑盒测试可以**原样指向新二进制**（见 §6.1）。

### 3.1 与现在相同、必须保持的外部约定

| 约定 | 内容 |
|---|---|
| 索引目录 | `<root>/.ctxindex/`，含 `index.db`（WAL）、`views/`、`stash/`、`backend.log` |
| SQLite schema | 见 §5.1，**迁移前后逐字相同** |
| 行号 | 1 基、闭区间；`chunks.s`/`chunks.e` 与 `symbols.line` 语义不变 |
| token 估算 | ASCII 约 3.6 字符/token，非 ASCII（中日韩）约 1 字符/token |
| 分块参数 | `MAX_CHUNK_LINES=120`、`MAX_CHUNK_CHARS=6000`、`MIN_CHUNK_LINES=15`、`LINE_CLIP=1000` |
| 文件类别 | `kind ∈ {code, doc, binary, huge}` |
| 配置 | 项目 `.ctxguard.json` < 用户 `~/.ctxguard.json`；环境变量 `CTXGUARD_*` |
| 配置优先级 | 环境变量 > 项目 > 用户 > 默认；`backends` 例外（安全策略见 §8.3） |
| MCP 协议 | JSON-RPC over stdio，`initialize` / `tools/list` / `tools/call`，5 个工具 |
| hook 协议 | PreToolUse 拦截 `Read`/`Grep`/`Bash`；拒绝时 exit 2 且 stderr 携带目录；SessionStart 注入目录概览 |
| 拒绝消息格式 | "拒绝原因 + 目录（起止行 | tokens | 标题 — 摘要）+ 下一步提示"，逐字不变 |

## 4. 切片定义

每个切片是一个**独立的可执行文件或库单元**，通过 **stdin/stdout 的 JSON** 与 Python 侧交互。
**Python 决定"做什么"，Rust 只负责"怎么做"**——职责边界划在数据上，不在消息上，
这样跨语言不会产生行为分叉。

### 切片 A：分块器

| 项 | 内容 |
|---|---|
| 职责 | 把一份文本切成"带标题与摘要的段"（现在的 `lib.chunk_text` 及其启发式，约 350 行） |
| 接口 | `ctxguard chunk --ext <ext> [--json]`：stdin = 文件内容，stdout = JSON `[{s,e,tok,title,summary}]` |
| Python 侧 | `lib.chunk_text` 先 `shutil.which("ctxguard-chunk")` 探测；在则调用并解析 JSON，不在则用现有实现 |
| 退场条件 | 对拍 100% 一致后，删掉 Python 侧的分块启发式 |
| 依赖 | 无（最底层） |
| 风险 | **中**：启发式正则含前瞻，Rust 的 `regex` crate 不支持（见 §8.1） |

### 切片 B：索引器

| 项 | 内容 |
|---|---|
| 职责 | 遍历仓库 → 调 A 切片切段 → 写 SQLite（现在的 `lib.build`/`index_one`/`list_files`/`open_db`） |
| 接口 | `ctxguard build --root <dir> [--force] [--quiet] [--time-limit SEC]`，行为与现有 `index.py build` 一致 |
| Python 侧 | `lib.build` 优先调它；不再则走原实现 |
| 退场条件 | 索引库内容与 Python 实现逐行一致（§6.2） |
| 依赖 | A |
| 风险 | 低（写库逻辑直白）；注意 `git ls-files` 的文件枚举与 `SKIP_DIRS` 剪枝要一致 |

### 切片 C：扫描器

| 项 | 内容 |
|---|---|
| 职责 | 对一批给定文件做正则扫描，产出命中行（现在的 `t_search` 的扫描段） |
| 接口 | `ctxguard scan --pattern <re> --json`：stdin = JSON `{"files": [...]}`，stdout = JSON `[{"path":..., "lines":[{"n":..,"text":..}]}]` |
| Python 侧 | `t_search` 负责决定"搜哪些文件"（复用 SQLite 的 `kind`/`size`/glob 规则），把清单交给 Rust 扫描；再把命中映射回段 |
| 退场条件 | 命中结果与 Python 实现逐条一致 |
| 依赖 | 无（只吃文件清单，不碰索引） |
| 风险 | 低。**关键设计**：文件清单由 Python 给，Rust 不自己决定搜什么 → 两边搜的文件集必然一致 |

### 切片 D：主 CLI

| 项 | 内容 |
|---|---|
| 职责 | `tree` / `outline` / `read` / `search` / `symbols` 五个命令（现在的 `tools.py` + `index.py`） |
| 接口 | 与现有 `index.py` 的子命令**逐字一致**（名字、参数、输出） |
| Python 侧 | 无（这是终态接口，不是被调用的） |
| 退场条件 | 144 项黑盒测试指向新二进制后全绿 |
| 依赖 | A、B、C |
| 风险 | 中。工具注册表（现在在 `tools.py` 的 `TOOLS`）在 Rust 里同样是一张静态表——**单源原则照搬** |

### 切片 E：hook 决策

| 项 | 内容 |
|---|---|
| 职责 | Read / Grep / Bash 的放行判断（现在的 `hook_pre.py`，约 360 行） |
| 接口 | `ctxguard guard`：stdin = hook payload，stdout = 决策与消息，exit 0（放行）/ 2（拒绝） |
| Python 侧 | 无 |
| 退场条件 | 40 条 Bash 用例 + Read/Grep 用例逐条一致；**且** 拒绝消息逐字一致 |
| 依赖 | 读 SQLite（渲染目录）→ 依赖 B 的索引格式 |
| 风险 | **高**（见 §8.2），**因此排在最后** |

### 切片 F：MCP 服务与代理

| 项 | 内容 |
|---|---|
| 职责 | `ctxguard mcp`（JSON-RPC over stdio）与 `ctxguard proxy -- CMD`（转发 + 大返回转存） |
| 接口 | 与现有 `server.py` / `proxy.py` 一致 |
| 退场条件 | MCP 五个工具的响应与代理行为一致 |
| 依赖 | D |
| 风险 | 低。长驻进程，启动开销无关紧要；**逻辑薄**，迁移收益主要在"少一个 Python 运行时" |

### 4.1 不迁移的部分（明确列出）

| 部分 | 理由 |
|---|---|
| `install.py` | 一次性脚本，要改用户的 `settings.json` / `.mcp.json` / `CLAUDE.md`，用 Python 或 shell 最省事。若终局要"零 Python"，可最后用 Rust 重写，但不是必须 |
| `tools.py` 的配置与注册表 | 数据量小、逻辑薄；注册表在 Rust 里只是一张静态表 |
| `server.py` / `proxy.py` 的 JSON-RPC 协议细节 | 长驻进程，无性能诉求；迁移主要是消除 Python 运行时 |

## 5. 接口契约

跨切片、跨语言必须稳定的东西。**这些是 Rust 实现与 Python 实现之间的契约，任何改动都要同步改两侧并更新本节。**

### 5.1 SQLite schema（不变）

```sql
CREATE TABLE IF NOT EXISTS files(path TEXT PRIMARY KEY, size INTEGER, mtime REAL, kind TEXT,
                                 lines INTEGER, tok INTEGER, nchunks INTEGER);
CREATE TABLE IF NOT EXISTS chunks(path TEXT, idx INTEGER, s INTEGER, e INTEGER, tok INTEGER,
                                  title TEXT, summary TEXT, PRIMARY KEY(path, idx));
CREATE TABLE IF NOT EXISTS symbols(path TEXT, line INTEGER, name TEXT);
CREATE INDEX IF NOT EXISTS sym_name ON symbols(name);
CREATE INDEX IF NOT EXISTS sym_path ON symbols(path);
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
```

`meta.sig` 存索引签名（`INDEX_VERSION` + 分块参数 + 后端签名）。后端迁到 tree-sitter 后，
签名内容会变——**旧索引必须自动失效重建**（现有机制已支持，改签名字符串即可）。

### 5.2 JSON 输出约定

- 编码 UTF-8，不转义非 ASCII（与现有 `json.dumps(..., ensure_ascii=False)` 一致）
- 段：`{"s": int, "e": int, "tok": int, "title": str, "summary": str}`，`s`/`e` 为 1 基闭区间
- 命中：`{"path": str, "lines": [{"n": int, "text": str}]}`
- 浮点：token 估算用整数；耗时等诊断数据不进协议输出

### 5.3 退出码

| 码 | 含义 |
|---|---|
| 0 | 成功 / hook 放行 |
| 1 | 用法错误（参数非法、路径不存在） |
| 2 | hook 拒绝（stderr 携带"拒绝原因 + 目录 + 下一步"） |

### 5.4 命令行表面

保持与现有 `index.py` 逐字一致（`build` / `tree` / `outline` / `read` / `search` / `symbols` /
`doctor` / `compare`，参数名与默认值不变），额外增加内部子命令 `chunk` / `scan` / `guard` /
`mcp` / `proxy`。**这是"144 项黑盒测试可以原样指向新二进制"的前提。**

## 6. 迁移顺序与验收

### 6.1 每一步的 Definition of Done

| 步 | 内容 | 验收 |
|---|---|---|
| 0 | 对拍护栏（纯 Python，不迁任何东西） | 见 §6.2 |
| 1 | 切片 A（分块器） | 对拍 100% 一致；现有 144 项测试全绿 |
| 2 | 切片 B（索引器） | 索引库三表与 Python 实现逐行一致；144 项全绿 |
| 3 | 切片 C（扫描器）+ 修掉 §1.3 的连接复用问题 | 命中结果逐条一致；search 耗时达标；144 项全绿 |
| 4 | 切片 D（主 CLI） | **144 项黑盒测试改指向二进制后全绿**；CLI 输出逐字一致 |
| 5 | 切片 F（MCP / 代理） | MCP 五个工具响应一致；代理转存行为一致 |
| 6 | 切片 E（hook） | Bash/Read/Grep 用例一致；拒绝消息逐字一致 |
| 7 | Python 侧退役 | 运行时零 Python；`install.py` 简化；README 更新 |

### 6.2 对拍护栏（步骤 0 的产物，必须先有）

一个脚本，对同一批输入跑两套实现，逐字段比对输出：

- **输入集**：`tests/gen_sample.py` 生成的样例仓库（覆盖 12 种语言与格式）
  + 真实 npm 源码树 + Python 标准库（2.8 万个 `.py`）
- **比对对象**（按切片）：
  - A：`chunk` 的 `s` / `e` / `tok` / `title` / `summary` **逐段逐字段**
  - B：`files` / `chunks` / `symbols` 三表**逐行**
  - C：命中集合（path + 行号 + 文本）**逐条**
  - D/E：命令输出**逐字节**
- **差异处理**：任何不一致都要归因到"实现差异"或"规格差异"。若规格本身不合理，
  先改规格（并同步改 Python 侧与本设计文档），再让两边一致——**不允许"改一边了事"**。

### 6.3 回归网

现有测试是纯黑盒的（驱动 `index.py` 子进程、跟 MCP 说 JSON-RPC、跟代理对管道），
**可以原封不动地指向新二进制**。这是本次迁移最大的风险缓冲：

> 迁移不是"重写后再想办法测"，而是"保持测试不变，换掉被测对象"。

需要额外补的是 Rust 自身的单元测试（并发、`unsafe`、整数溢出这些 Python 侧不存在的问题），
尤其是分块启发式（切片 A）——它目前只有间接覆盖。

## 7. 行为等价性机制

| 层次 | 手段 | 覆盖 |
|---|---|---|
| 外部行为 | 144 项黑盒测试 | 端到端：hook 拒绝、MCP 响应、CLI 输出、索引一致性、预算、转存 |
| 切片行为 | 对拍护栏（§6.2） | 逐字段/逐行/逐字节 |
| 单元行为 | Rust 单元测试 + cargo test | 启发式边界、序列化、错误处理 |

## 8. 风险登记

### 8.1 分块启发式的正则含前瞻（切片 A）

现状用了 lookaround：

- `_CTRL`：`(?!\s*(?:if|else|for|while|switch|...)\b)`
- `LEAD`：`#(?!\s*(?:include|define|pragma|if|ifdef|...)\b)`
- `DECL_METHOD` / `DECL_C_SIG`：以 `^` + 复杂字符类为主的模式

Rust 的 `regex` crate **不支持前瞻/后顾**。三条路：

1. 用 `fancy-regex`（支持前瞻）——但比 `regex` 慢，且它是"回溯"实现，长输入上可能退化
2. 把前瞻改写成等价的"先取候选、再过滤"逻辑——更快，但要逐条论证等价
3. 混合：能改写的改写，实在改不动的用 `fancy-regex`

**倾向 2**，因为改写后的逻辑更直白，且能顺带去掉对回溯引擎的依赖。
但每一条改写都必须有对拍用例覆盖——这是 §6.2 存在的理由。

### 8.2 hook 的 Bash 分析语义（切片 E）

`hook_pre._segments()` 用 `shlex.shlex(cmd, posix=True, punctuation_chars=True)` 切分命令。
Rust 的 `shell-words` 等价物**没有** `punctuation_chars` 语义，切分结果会不同
（例如 `a&&b`、`a>b` 的边界）。这是全项目最容易出微妙差异的地方。

缓解：现有 40 条 Bash 用例（含 `cat > x.txt <<'EOF'`、`sudo`、`env FOO=1`、`2>/dev/null;`、
未闭合引号等边界）正好是现成的对照集，作为 Rust 侧的测试数据逐条移植。

### 8.3 明确的功能倒退（已决策接受）

1. **自定义 Python 后端插件作废**。现在支持 `plugin:/绝对路径.py` 与任意可导入模块名，
   用于声明提取后端。Rust 侧不做插件机制（若要保留，需走 dylib ABI，复杂度陡增）。
   连带作废的还有"项目级配置只接受内置后端名"这条安全策略（因为不再有插件可加载）。
2. **pyast 后端作废**，后端统一用 tree-sitter。代价：失去"零依赖的精确 Python 分块"
   （tree-sitter 是必需依赖）。收益：tree-sitter 原生支持 Rust，
   `backend_ts.py` 目前"只用假节点测过、真实语法节点名未实测"的状态可以在 Rust 侧一次性解决。
3. **声明提取后端的版本与选项**（`descend_functions`、`max_depth`）语义保持不变，
   但索引签名（`meta.sig`）会变 → 旧索引自动失效重建。

### 8.4 其他

| 风险 | 影响 | 缓解 |
|---|---|---|
| 分发：二进制要按平台构建 | macOS arm64/x86_64、Linux gnu/musl、Windows | 渐进期"可选安装"（见 §10）；终局由 CI 出多平台产物 |
| 行为漂移 | 模型看到的目录与拒绝消息变了 | 对拍护栏 + 144 项黑盒测试；差异必须归因 |
| 迁移期双实现并存 | 代码量临时增加，看起来更复杂 | 明确这是**过渡态**；每片迁完即删 Python 侧 |
| 树结构启发式的隐性知识 | `lib.py` 的 `_split_region` / `_merge` / `_windows` 逻辑微妙 | 迁移前先补 Python 侧的单元测试，把现有行为钉死 |
| 插件用户 | 少数依赖自定义后端的人会失去能力 | README 明确说明；如确有需求，可保留一个 Python 侧"后端子进程"作为过渡 |

## 9. 明确不迁的部分

见 §4.1。此外：

- **SQLite schema**：不变（§5.1）
- **索引目录布局**：不变（`.ctxindex/`）
- **MCP 工具的五个名字与语义**：不变
- **CLI 的子命令与参数**：不变
- **拒绝消息的文案与排版**：不变（逐字）

## 10. 分发与降级策略

### 10.1 渐进期：可选安装 + Python 兜底

每个 Rust 片都是独立可执行文件，Python 侧用 `shutil.which()` 探测：

```
探测到 ctxguard-chunk  → 调它
探测不到                → 用 Python 实现
```

好处：
- **纯 Python 用户完全不受影响**——不需要装任何东西，行为不变
- 迁移可以任意暂停或回退
- 分发问题被降级成"可选安装"，不必一次解决
- 每个 Rust crate 可以独立发版，与 Python 侧版本节奏解耦

### 10.2 终局

全部迁完后：
- `install.py` 变成"复制一个二进制 + 写配置"
- Python 版本下限、解释器探测、导入失败 fail-open 那一整套**全部删除**
- hook 命令变成 `ctxguard guard`，MCP 命令变成 `ctxguard mcp`

### 10.3 关于"打包"

此前评估过用 PyInstaller / Nuitka 把 Python 侧打成二进制（同样能消除解释器依赖）。
**本次不走这条路**——它不解决"类型由编译器保证"和"单一实现"，只是换了个分发方式。
Rust 迁移顺带解决这两点，所以不再单独做 Python 打包。

## 11. 迁移前可独立收获的改进（与 Rust 无关）

这些现在就能做，且**应该先做**——否则迁移的收益会被实现缺陷掩盖：

1. **search 的连接复用**：`_hit_chunks()` 每个命中文件重开一次 SQLite。
   改为复用连接 + 批量查询 → 21.4 s → ~12 s（纯 Python）。
2. **search 的超时预算**：现状 15 s 会截断结果，模型拿到残缺命中却不自知。
   要么提高预算，要么在超时后明确告知"已截断"（现状有 `timed_out` 标记，但输出里没说清楚）。
3. **分块启发式的单元测试**：现状只有间接覆盖。迁移前先在 Python 侧钉死行为。

## 12. 已决策与待定

### 已决策（2026-11）

| 决策 | 内容 |
|---|---|
| D1 | 渐进迁移到 Rust，不做一次性重写 |
| D2 | 切分为 A–F 六片，按"可交付、可回退"推进，Rust 片之间用 JSON 通信 |
| D3 | **接受自定义 Python 后端插件作废** |
| D4 | **后端统一用 tree-sitter**，pyast 后端作废 |
| D5 | 性能不是目标，单一实现与无解释器依赖才是 |
| D6 | 行为不变是硬约束，144 项黑盒测试是验收标准 |
| D7 | 先写设计文档，评审后再开工 |

### 待定

| # | 问题 | 影响 |
|---|---|---|
| Q1 | 切片 A 的正则前瞻：改写（倾向）还是用 `fancy-regex`？ | 决定 A 的实现难度与性能特征 |
| Q2 | 迁移期是否保留 Python 侧的"后端子进程"作为插件过渡？ | 影响 D3 的实际冲击面 |
| Q3 | 二进制的多平台产物由谁出（本地交叉编译 / CI）？ | 影响终局的分发可行性 |
| Q4 | 切片 E 的 hook 是否值得迁？（它风险最高，而收益只是 27 ms → 3 ms） | 可能最终保留 Python hook，只迁其余 |
| Q5 | 是否把"拒绝消息渲染"也放进 Rust（需要复刻 `outline_text` 格式） | 影响 E 的行为分叉风险 |

## 附录 A：Rust 工程布局

```
crates/ctxguard/
  Cargo.toml
  src/
    lib.rs           # 库入口；类型定义、token 估算
    chunk.rs         # 切片 A：分块启发式
    index.rs         # 切片 B：索引
    scan.rs          # 切片 C：扫描
    cli/             # 切片 D：五个命令（注册表单源）
    guard.rs         # 切片 E：hook 决策
    mcp.rs           # 切片 F：MCP 服务
    proxy.rs         # 切片 F：代理
    bin/ctxguard.rs  # 主程序（子命令分发）
tests/              # Rust 单元测试 + 移植的 Bash 用例
tools/parity.py     # 对拍护栏（§6.2）
```

Python 侧的目录结构在迁移完成前保持不变（12 个平铺模块 + `tests/`）。

## 附录 B：现状与终局的对照

| 维度 | 现状 | 终局 |
|---|---|---|
| 运行时依赖 | Python 3.13+（用户机器） | 无（单二进制） |
| 投放物 | 11 个 `.py` + 配置 | 1 个二进制 + 配置 |
| 类型检查 | ruff + ty（靠标注自觉） | 编译器强制 |
| hook / MCP / CLI 实现 | 三处（tools.py 共享工具层，但入口各自拼装） | 一处 |
| 解释器探测 | install.py ~50 行 | 无 |
| 导入失败 fail-open | 存在（已修一处） | 不存在（无导入期） |
| 坏配置 | 已改为退回默认 + 警告 | 启动期即可硬失败 |
| search（53k 文件） | 21.4 s（超时截断） | 目标 < 3 s |
| hook 延迟 | 27.2 ms | 目标 < 5 ms |
| 索引（53k 文件） | 45.8 s（建不完） | 目标 < 12 s |
