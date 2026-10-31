# -*- coding: utf-8 -*-
"""统一路径解析 —— 让整个工程可以在任意盘符、任意文件夹下运行。

打包发给朋友时，ComfyUI 可能在 D:\\、可能在 C:\\Program Files\\、
也可能装在任意文件夹里。工程里所有指向 ComfyUI 的绝对路径都集中到
这里解析，按以下优先级找：

  1. 环境变量（最优先，方便临时切换）
       ATLAS_COMFY_ROOT  —— ComfyUI 安装根目录（含 python_embeded 的那一层）
       COMFY_ROOT        —— 同上，兼容别人的习惯命名
       ATLAS_COMFY_API   —— ComfyUI 的 API 地址，如 http://127.0.0.1:8188
  2. 配置文件（app/config.json 的 comfy_root / comfy_api 字段）
  3. 自动扫描：遍历本机所有盘符，找常见的 ComfyUI 目录特征
  4. 兜底：PATH 里的 python、127.0.0.1:8188

「ComfyUI 根目录」指的是同时含有下面两样东西的那一层目录：
    <root>/python_embeded/python.exe   （Windows 便携版的解释器）
    <root>/ComfyUI/main.py             （ComfyUI 主程序）
有些便携包把 ComfyUI 直接解压在根目录（<root>/main.py），也一并识别。
"""

import json
import os
import sys

# ------------------------------------------------------------------ 小工具

def _norm(p):
    """统一成绝对路径 + 正斜杠，便于比较。"""
    if not p:
        return ""
    p = os.path.expandvars(os.path.expanduser(str(p).strip().strip('"')))
    if not p:
        return ""
    p = str(p).replace("\\", "/")
    if len(p) > 2 and p[1] == ":":
        p = p[0].upper() + p[1:]
    return p


def _is_dir(p):
    try:
        return bool(p) and os.path.isdir(p)
    except OSError:
        return False


def _is_file(p):
    try:
        return bool(p) and os.path.isfile(p)
    except OSError:
        return False


def _read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:                                  # noqa: BLE001
        return {}


def drive_letters():
    """本机所有可用的盘符（Windows）。"""
    out = []
    if os.name != "nt":
        return ["/"]
    try:
        import ctypes
        mask = ctypes.windll.kernel32.GetLogicalDrives()
        for i in range(26):
            if mask & (1 << i):
                out.append("%s:" % chr(65 + i))
    except Exception:                                  # noqa: BLE001
        pass
    return out or ["C:"]


# ------------------------------------------------------- ComfyUI 位置探测

# ComfyUI 目录名的常见特征（不区分大小写）
_NAME_HINTS = ("comfyui", "comfy-ui", "comfy_ui", "comfy ui")
# 根目录里常见的子目录名
_SUB_DIRS = ("ComfyUI", "comfyui", "comfy", "ComfyUI_windows_portable",
             "ComfyUI_portable", "stable-diffusion-webui")


def _looks_like_comfy_root(d):
    """这个目录像不像 ComfyUI 的根目录（便携包那一层）。"""
    if not _is_dir(d):
        return False
    d = d.rstrip("/")
    # 特征 1：有 ComfyUI/main.py 或直接在根目录的 main.py
    if _is_file(d + "/ComfyUI/main.py") or _is_file(d + "/main.py"):
        return True
    # 特征 2：有 ComfyUI/custom_nodes 或 models 目录
    if _is_dir(d + "/ComfyUI/custom_nodes") or _is_dir(d + "/custom_nodes"):
        return True
    return False


def _python_in(root):
    """在 ComfyUI 根目录里找 python 解释器。"""
    cands = [
        root + "/python_embeded/python.exe",
        root + "/python_embeded/pythonw.exe",
        root + "/embedded/python.exe",
        root + "/venv/Scripts/python.exe",
        root + "/.venv/Scripts/python.exe",
    ]
    for c in cands:
        if _is_file(c):
            return c
    return ""


def _comfy_main(root):
    """在 ComfyUI 根目录里找 ComfyUI 主程序所在目录。"""
    for c in (root + "/ComfyUI", root):
        if _is_file(c + "/main.py"):
            return c
    return ""


def _scan_for_comfy():
    """扫描所有盘符，找 ComfyUI 根目录。

    先扫盘根目录（深度 1），命中概率最高、速度最快；
    找不到再扫根目录下一层（深度 2），覆盖 D:\\AI\\ComfyUI 这类结构。
    """
    hits = []
    for dl in drive_letters():
        base = dl + "/"
        if not _is_dir(base):
            continue
        try:
            entries = os.listdir(base)
        except OSError:
            continue
        # 深度 1：<盘>\\ComfyUI_xxx
        for name in entries:
            d = base + name
            if _looks_like_comfy_root(d):
                hits.append(d)
        # 深度 2：<盘>\\任意\\ComfyUI_xxx
        for name in entries:
            sub = base + name
            if not _is_dir(sub):
                continue
            try:
                subs = os.listdir(sub)
            except OSError:
                continue
            for s2 in subs:
                d = sub + "/" + s2
                if _looks_like_comfy_root(d):
                    hits.append(d)
    # 去重，优先路径里带 comfy 字样的
    uniq = []
    for h in hits:
        if h not in uniq:
            uniq.append(h)
    uniq.sort(key=lambda p: (0 if any(h in p.lower() for h in _NAME_HINTS) else 1, len(p)))
    return uniq


def find_comfy(config=None, cache=None):
    """定位 ComfyUI。返回 dict:
        {root, comfy_dir, python, api, source}
    source 说明是靠哪一级找到的，便于排障。
    """
    cfg = config or {}
    cache = cache if cache is not None else {}

    # --- 1. 环境变量 ---------------------------------------------------
    env_root = os.environ.get("ATLAS_COMFY_ROOT") or os.environ.get("COMFY_ROOT")
    if env_root and _looks_like_comfy_root(_norm(env_root)):
        root = _norm(env_root)
        return {"root": root, "comfy_dir": _comfy_main(root),
                "python": _python_in(root), "api": "http://127.0.0.1:8188",
                "source": "环境变量", "candidates": [root]}

    # --- 2. 配置文件 ---------------------------------------------------
    cf_root = _norm(cfg.get("comfy_root"))
    if cf_root and _looks_like_comfy_root(cf_root):
        return {"root": cf_root, "comfy_dir": _comfy_main(cf_root),
                "python": _python_in(cf_root),
                "api": cfg.get("comfy_api") or "http://127.0.0.1:8188",
                "source": "配置文件", "candidates": [cf_root]}

    # --- 3. 自动扫描 ---------------------------------------------------
    if "scanned" not in cache:
        cache["scanned"] = _scan_for_comfy()
    if cache["scanned"]:
        root = cache["scanned"][0]
        return {"root": root, "comfy_dir": _comfy_main(root),
                "python": _python_in(root),
                "api": cfg.get("comfy_api") or "http://127.0.0.1:8188",
                "source": "自动扫描",
                "candidates": cache["scanned"]}

    # --- 4. 兜底 -------------------------------------------------------
    return {"root": "", "comfy_dir": "", "python": "",
            "api": cfg.get("comfy_api") or "http://127.0.0.1:8188",
            "source": "未找到", "candidates": []}


# ------------------------------------------------------- 姿态库/服装库位置

def find_data_dir(kind, cfg, app_dir):
    """定位姿态库/服装库。顺序：环境变量 → 配置（相对/绝对）→ 同级目录扫描。"""
    env = os.environ.get("ATLAS_%s_DIR" % kind.upper())
    if env and _is_dir(_norm(env)):
        return _norm(env)

    raw = cfg.get("%s_dir" % kind)
    if raw:
        p = _norm(raw)
        if not os.path.isabs(p):
            p = _norm(os.path.join(app_dir, p))
        if _is_dir(p):
            return p

    # 打包后常见布局：<包根>/data/pose-atlas  或  <包根>/data/costume-gallery
    base = os.path.dirname(app_dir.rstrip("/"))
    for cand in (os.path.join(base, "data", "%s-gallery" % kind),
                 os.path.join(base, "data", kind),
                 os.path.join(app_dir, "data", "%s-gallery" % kind)):
        c = _norm(cand)
        if _is_dir(c):
            return c
    return _norm(os.path.join(base, "data", "%s-gallery" % kind))


# ------------------------------------------------------------------ 汇总

def resolve(app_dir, config_path=None):
    """一次性解析出工程需要的全部路径。"""
    app_dir = _norm(app_dir)
    cfg_path = config_path or os.path.join(app_dir, "config.json")
    cfg = _read_json(cfg_path)

    comfy = find_comfy(cfg)

    api = os.environ.get("ATLAS_COMFY_API") or os.environ.get("COMFY_API") \
        or comfy.get("api") or "http://127.0.0.1:8188"
    comfy["api"] = api

    return {
        "app_dir": app_dir,
        "config": cfg,
        "config_path": cfg_path,
        "comfy": comfy,
        "pose_dir": find_data_dir("pose", cfg, app_dir),
        "costume_dir": find_data_dir("costume", cfg, app_dir),
    }


def diagnose(info):
    """给启动器看的一段人类可读诊断信息。"""
    c = info["comfy"]
    lines = []
    lines.append("ComfyUI 根目录 : %s" % (c["root"] or "[未找到]"))
    lines.append("  来源        : %s" % c["source"])
    lines.append("ComfyUI 主程序: %s" % (c["comfy_dir"] or "[未找到]"))
    lines.append("Python 解释器: %s" % (c["python"] or "[未找到，将用系统 python]"))
    lines.append("ComfyUI API  : %s" % c["api"])
    lines.append("姿态库       : %s%s" % (info["pose_dir"],
                                          "" if _is_dir(info["pose_dir"]) else "  [缺失]"))
    lines.append("服装库       : %s%s" % (info["costume_dir"],
                                          "" if _is_dir(info["costume_dir"]) else "  [缺失]"))
    cands = c.get("candidates") or []
    if len(cands) > 1:
        lines.append("其他候选位置 :")
        for x in cands[1:]:
            lines.append("  - %s" % x)
        lines.append("如需指定其它位置：设置环境变量 ATLAS_COMFY_ROOT")
    return "\n".join(lines)


def remember(info, config_path):
    """把自动扫描到的 ComfyUI 路径写回 config.json —— 下次启动直接命中，不用再扫盘。"""
    c = info.get("comfy") or {}
    if c.get("source") != "自动扫描":
        return False
    root = c.get("root")
    if not root:
        return False
    cfg = _read_json(config_path)
    if cfg.get("comfy_root") == root:
        return False
    cfg["comfy_root"] = root
    try:
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        return True
    except OSError:
        return False


if __name__ == "__main__":
    app = sys.argv[1] if len(sys.argv) > 1 else os.path.dirname(os.path.abspath(__file__))
    info = resolve(app)
    # 把自动扫描的结果记进 config.json，下次启动就不用再扫盘了
    if remember(info, info["config_path"]):
        info = resolve(app)          # 重新解析，来源会变成「配置文件」
    # 机器可读段：BAT 用 findstr 解析这里的 key=value
    print("[atlas]")
    print("comfy_root=%s" % info["comfy"]["root"])
    print("comfy_dir=%s" % info["comfy"]["comfy_dir"])
    print("python=%s" % info["comfy"]["python"])
    print("api=%s" % info["comfy"]["api"])
    print("pose_dir=%s" % info["pose_dir"])
    print("costume_dir=%s" % info["costume_dir"])
    print("source=%s" % info["comfy"]["source"])
    print("[/atlas]")
    print("")
    print(diagnose(info))
