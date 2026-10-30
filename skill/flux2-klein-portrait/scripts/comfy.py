# -*- coding: utf-8 -*-
"""Shared ComfyUI API helpers for the flux2-klein-portrait skill.

All graph builders in this skill target the local ComfyUI instance described in
config.json and the custom node PainterFluxImageEdit (Comfyui-PainterFluxImageEdit).
"""
import json
import os
import re
import shutil
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CFG_PATH = os.path.join(ROOT, "config.json")

with open(CFG_PATH, encoding="utf-8") as _f:
    C = json.load(_f)

API = os.environ.get("COMFY_API") or C["api"]
COMFY = C["comfy_root"]
IN_DIR = os.path.join(COMFY, "input")
OUT_DIR = os.path.join(COMFY, "output")
DEF = C["defaults"]


# --------------------------------------------------------------------------- io
def get(path, timeout=60):
    with urllib.request.urlopen(API + path, timeout=timeout) as r:
        return json.loads(r.read().decode())


def post(path, obj, timeout=120):
    req = urllib.request.Request(
        API + path, data=json.dumps(obj).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def alive():
    """True when the ComfyUI server answers. Always check before a batch."""
    try:
        get("/system_stats", 5)
        return True
    except Exception:
        return False


def require_server():
    if not alive():
        raise SystemExit(
            "ComfyUI 未响应 %s\n先启动：E:/ComfyUI_Mie_V33/1_1点击启动comfyui.bat" % API)


# ------------------------------------------------------------------- graph parts
def loaders(unet=None, clip=None, vae=None):
    """Standard FLUX.2 Klein loader trio -> {node_id: node}."""
    return {
        "1": {"class_type": "UNETLoader", "inputs": {
            "unet_name": unet or C["models"]["unet"], "weight_dtype": "default"}},
        "2": {"class_type": "CLIPLoader", "inputs": {
            "clip_name": clip or C["models"]["clip"], "type": "flux2", "device": "default"}},
        "3": {"class_type": "VAELoader", "inputs": {
            "vae_name": vae or C["models"]["vae"]}},
    }


def ref(node, out=0):
    """Link helper: accept "12" or ("12", 1) or ["12", 1]."""
    if isinstance(node, (list, tuple)):
        return [str(node[0]), int(node[1])]
    return [str(node), out]


def sampler(model, positive, negative, latent, seed, steps, cfg=None,
            sampler_name=None, scheduler=None, denoise=None):
    return {
        "model": ref(model, 0), "positive": ref(positive, 0), "negative": ref(negative, 1),
        "latent_image": ref(latent, 0), "seed": int(seed), "steps": int(steps),
        "cfg": DEF["cfg"] if cfg is None else cfg,
        "sampler_name": sampler_name or DEF["sampler"],
        "scheduler": scheduler or DEF["scheduler"],
        "denoise": DEF["denoise"] if denoise is None else denoise,
    }


def cleanup(nodes, ids):
    """Remove nodes whose id starts with any of the given prefixes."""
    for nid in list(nodes):
        if any(str(nid).startswith(str(p)) for p in ids):
            nodes.pop(nid, None)


# ----------------------------------------------------------------------- staging
def _safe(name):
    return re.sub(r"[^A-Za-z0-9._-]", "_", name)


def stage(path, name=None):
    """Copy a local file into ComfyUI/input (LoadImage only reads from there).

    Returns the basename to put into LoadImage.image. Already-staged names pass
    through unchanged, so callers can pass either a local path or a bare name.
    """
    if not path:
        return path
    if name is None and not os.path.isabs(path) and os.path.exists(os.path.join(IN_DIR, path)):
        return path
    if not os.path.exists(path):
        raise FileNotFoundError(path)
    base = name or _safe(os.path.basename(path))
    dst = os.path.join(IN_DIR, base)
    if os.path.abspath(path) != os.path.abspath(dst):
        shutil.copy2(path, dst)
    return base


# --------------------------------------------------------------------- execution
def submit(graph, timeout=3600, poll=2.0, verbose=True):
    """Queue one graph, block until done, return the emitted image records."""
    require_server()
    try:
        pid = post("/prompt", {"prompt": graph})["prompt_id"]
    except urllib.error.HTTPError as e:
        raise RuntimeError("ComfyUI 拒绝该节点图 %s: %s" % (e.code, e.read().decode()[:1500]))

    t0 = time.time()
    while time.time() - t0 < timeout:
        hist = get("/history/%s" % pid)
        if pid in hist:
            status = hist[pid].get("status", {})
            imgs = [im for o in hist[pid].get("outputs", {}).values()
                    for im in o.get("images", [])]
            if verbose:
                print("[%s] %.1fs %s -> %s" % (
                    pid[:8], time.time() - t0, status.get("status_str"),
                    [i["filename"] for i in imgs]), flush=True)
            if status.get("status_str") == "error":
                raise RuntimeError(json.dumps(status.get("messages"), ensure_ascii=False)[:2000])
            return imgs
        time.sleep(poll)
    raise TimeoutError("prompt %s 超过 %ss 未完成" % (pid, timeout))


def out_path(im):
    return os.path.join(OUT_DIR, im.get("subfolder") or "", im["filename"])


def pull(images, outdir, stem=None, ext=".jpg", quality=96):
    """Copy/convert ComfyUI outputs into a normal folder. Returns saved paths."""
    from PIL import Image as _I
    os.makedirs(outdir, exist_ok=True)
    saved = []
    for i, im in enumerate(images):
        src = out_path(im)
        name = stem or os.path.splitext(im["filename"])[0]
        if len(images) > 1:
            name = "%s_%02d" % (name, i + 1)
        dst = os.path.join(outdir, name + ext)
        if ext.lower() in (os.path.splitext(src)[1].lower(), ""):
            shutil.copy2(src, dst)
        else:
            img = _I.open(src).convert("RGB")
            img.save(dst, quality=quality, subsampling=0)
        saved.append(dst)
    return saved


def parse_size(text):
    """'864x1536' -> (864, 1536)."""
    w, h = str(text).lower().split("x")
    return int(w), int(h)


def clamp_size(w, h, minimum=512, multiple=8):
    """PainterFluxImageEdit refuses width/height below 512 or off the 8 grid.

    Scales both dimensions proportionally when the small side is too small, so
    aspect ratio survives.
    """
    w, h = int(w), int(h)
    if min(w, h) < minimum:
        k = minimum / float(min(w, h))
        w, h = int(w * k), int(h * k)
    w = max(minimum, w - w % multiple)
    h = max(minimum, h - h % multiple)
    return w, h
