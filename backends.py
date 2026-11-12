#!/usr/bin/env python3
"""ctxguard 声明提取后端：注册表、校验、签名、日志。

后端是一个模块，约定：
    NAME: str
    VERSION: str                              # 变更会使索引失效并重建
    supports(ext: str) -> bool                # ext 形如 ".py"（小写）
    declarations(text: str, ext: str, opts: dict) -> list | None
        返回 [(起始行0基, 层级, 签名文本, 结束行0基或None), ...]
        起始行应包含装饰器/export 等前导；层级 0 为顶层，类成员为 1，依此类推；
        返回 None 表示“我处理不了这个文件”，注册表会回退到下一个后端或内置启发式。

任何异常、非法返回值、超时预算都不会影响会话：一律回退（并写入 .ctxindex/backend.log）。

安全：配置里的后端名只有内置名（pyast、treesitter）允许写在“项目级” .ctxguard.json 里；
任意模块名或 plugin:<路径> 只接受来自用户级 ~/.ctxguard.json 或环境变量 CTXGUARD_BACKENDS，
因为项目文件可能来自不可信仓库，不能让它决定加载哪段代码。
"""
import functools
import importlib
import importlib.util
import json
import os
import time

BUILTIN = {"pyast": "backend_pyast", "treesitter": "backend_ts"}
AUTO = ["treesitter", "pyast"]
LOG_PATH: str | None = None   # 由 tools.load_config 设置
MIN_EMPTY_FALLBACK_LINES = 60   # 后端返回空列表且文件不小于此行数时，视为“没认出来”，回退启发式

_cache = {}
_errors = {}
_logged = set()


def expand(spec):
    """把配置值规整成有序名字列表。支持 "auto"、"a,b"、["a","b"]、"off"/空。"""
    if spec is None:
        return []
    if isinstance(spec, str):
        spec = [x.strip() for x in spec.split(",")]
    out = []
    for x in spec:
        if not isinstance(x, str) or not x.strip():
            continue
        x = x.strip()
        if x.lower() in ("off", "none", "false"):
            return []
        if x.lower() == "auto":
            out.extend(AUTO)
        else:
            out.append(x)
    seen, res = set(), []
    for x in out:
        if x not in seen:
            seen.add(x)
            res.append(x)
    return res


def is_builtin(name):
    return name in BUILTIN


def _log(kind, name, detail):
    key = (kind, name, detail[:80])
    if key in _logged:
        return
    _logged.add(key)
    if not LOG_PATH:
        return
    try:
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        if os.path.exists(LOG_PATH) and os.path.getsize(LOG_PATH) > 200_000:
            os.remove(LOG_PATH)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write("%s [%s] %s: %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), kind, name, detail))
    except OSError:
        pass


def last_errors():
    return dict(_errors)


def load(name):
    """加载后端模块；失败返回 None，并把原因记在 last_errors()。"""
    if name in _cache:
        return _cache[name]
    mod = None
    try:
        if name in BUILTIN:
            mod = importlib.import_module(BUILTIN[name])
        elif name.startswith("plugin:"):
            path = os.path.abspath(os.path.expanduser(name[7:]))
            spec = importlib.util.spec_from_file_location("ctxguard_plugin_%x" % (hash(path) & 0xFFFFFF), path)
            if spec is None or spec.loader is None:
                raise ImportError("无法为插件创建模块规格：%s" % path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
        else:
            mod = importlib.import_module(name)
        for attr in ("NAME", "VERSION", "supports", "declarations"):
            if not hasattr(mod, attr):
                raise AttributeError("后端缺少必需成员 %s" % attr)
    except Exception as ex:  # noqa: BLE001
        _errors[name] = "%s: %s" % (type(ex).__name__, ex)
        _log("load", name, _errors[name])
        mod = None
    _cache[name] = mod
    return mod


def validate(raw):
    """规整后端返回值；格式不对返回 None。"""
    if raw is None:
        return None
    out = []
    try:
        for d in raw:
            s, lv, txt = int(d[0]), int(d[1]), str(d[2])
            e = int(d[3]) if len(d) > 3 and d[3] is not None else None
            out.append([s, lv, " ".join(txt.split())[:120], e])
    except Exception:  # noqa: BLE001
        return None
    return out


def _finish(decls, nlines):
    res, seen = [], set()
    for s, lv, txt, e in sorted(decls, key=lambda x: (x[0], x[1])):
        if not (0 <= s < nlines) or lv < 0:
            continue
        if e is not None:
            e = None if e < s else min(e, nlines - 1)
        if (s, lv) in seen:
            continue
        seen.add((s, lv))
        res.append((s, lv, txt, e))
        if len(res) >= 20000:
            break
    return res


def get_decls(text, ext, names, opts, nlines):
    """按顺序询问后端，返回第一个可用的声明列表；全部不可用返回 None（调用方回退启发式）。"""
    for name in names:
        mod = load(name)
        if mod is None:
            continue
        try:
            if not mod.supports(ext):
                continue
            raw = mod.declarations(text, ext, dict(opts))
        except Exception as ex:  # noqa: BLE001
            _log("run", name, "%s %s: %s" % (ext, type(ex).__name__, ex))
            continue
        decls = validate(raw)
        if decls is None:
            if raw is not None:
                _log("run", name, "%s 返回了非法格式，已忽略" % ext)
            continue
        decls = _finish(decls, nlines)
        if not decls and nlines >= MIN_EMPTY_FALLBACK_LINES:
            continue
        return decls
    return None


def pkg_version(*dists):
    """若干发行包名 -> 'a=1.2,b=-'；用元数据，不真正导入包（便宜）。"""
    from importlib import metadata
    out = []
    for d in dists:
        try:
            out.append("%s=%s" % (d, metadata.version(d)))
        except Exception:  # noqa: BLE001
            out.append("%s=-" % d)
    return ",".join(out)


@functools.lru_cache(maxsize=16)
def _sig_one(name):
    try:
        if name in BUILTIN:
            mod = importlib.import_module(BUILTIN[name])
            extra = getattr(mod, "signature_extra", None)
            return "%s@%s%s" % (name, getattr(mod, "VERSION", "?"), ("(" + extra() + ")") if extra else "")
        if name.startswith("plugin:"):
            p = os.path.abspath(os.path.expanduser(name[7:]))
            st = os.stat(p)
            return "%s@%d:%d" % (name, st.st_size, int(st.st_mtime))
        mod = load(name)
        return "%s@%s" % (name, getattr(mod, "VERSION", "unavailable") if mod else "unavailable")
    except Exception:  # noqa: BLE001
        return "%s@unavailable" % name


def signature(names, opts):
    """索引签名：后端、版本、依赖包版本、选项。任何一项变化都使旧索引失效。"""
    if not names:
        return "none"
    o = {k: opts[k] for k in sorted(opts) if not k.startswith("_")}
    return "|".join(_sig_one(n) for n in names) + "|" + json.dumps(o, sort_keys=True)
