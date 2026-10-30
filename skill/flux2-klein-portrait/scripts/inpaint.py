# -*- coding: utf-8 -*-
"""Inpaint / local redraw: remove a watermark, fix shoes and feet, repair hands.

Two modes
---------
1. Whole-image inpaint at original resolution (mask = full-size white area):
   python inpaint.py --in shot.jpg --mask wm_mask.png --prompt "..." --seed 3
2. Zoom-redraw: crop a region with context, upscale it, redraw only the inner
   box, then feather-paste back. This is the reliable way to fix small defects
   (shoes, feet, fingers) because the model sees well-resolved detail.
   python inpaint.py --in shot.jpg --box 0.35,0.86,0.65,0.99 --zoom 2.2 --prompt "..." --seed 3

Notes
-----
* image1_mask must be a MASK type, produced by ImageToMask(red) from a grayscale
  PNG. Feeding a raw IMAGE raises HTTP 400.
* box coordinates are relative 0-1: x0,y0,x1,y1.
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import comfy as C


def _mask_png(size, box, feather):
    """Grayscale mask PNG in ComfyUI/input. box in absolute pixels."""
    from PIL import Image, ImageDraw, ImageFilter
    m = Image.new("L", size, 0)
    ImageDraw.Draw(m).rectangle(box, fill=255)
    if feather:
        m = m.filter(ImageFilter.GaussianBlur(feather))
    return m


def build(img, mask, w, h, prompt, seed, prefix, steps=8):
    g = C.loaders()
    g["10"] = {"class_type": "LoadImage", "inputs": {"image": img}}
    g["11"] = {"class_type": "LoadImage", "inputs": {"image": mask}}
    g["12"] = {"class_type": "ImageToMask", "inputs": {"image": ["11", 0], "channel": "red"}}
    g["20"] = {"class_type": "PainterFluxImageEdit", "inputs": {
        "clip": ["2", 0], "vae": ["3", 0], "prompt": prompt, "mode": "1_image",
        "batch_size": 1, "width": int(w), "height": int(h),
        "image1": ["10", 0], "image1_mask": ["12", 0]}}
    g["30"] = {"class_type": "KSampler", "inputs": C.sampler("1", ("20", 0), ("20", 1), ("20", 2), seed, steps)}
    g["40"] = {"class_type": "VAEDecode", "inputs": {"samples": ["30", 0], "vae": ["3", 0]}}
    g["50"] = {"class_type": "SaveImage", "inputs": {"images": ["40", 0], "filename_prefix": prefix}}
    return g


def _align(v, m=16):
    return max(m, int(round(v / float(m))) * m)


def run_one(job, outdir):
    from PIL import Image

    src = job["in"]
    tag = job.get("prefix") or os.path.splitext(os.path.basename(src))[0] + "_fix"
    im = Image.open(src).convert("RGB")
    W, H = im.size
    box = job.get("box")
    zoom = float(job.get("zoom", 1.0))
    feather = int(job.get("feather", 14))
    steps = int(job.get("steps", 8))

    if zoom <= 1.01:
        # ---- whole image, mask file or box provided by caller
        if job.get("mask"):
            mask_src = C.stage(job["mask"])
            target = C.stage(src, "inp_%s.png" % tag)
            w, h = W - W % 8, H - H % 8
            mask_name = mask_src
        else:
            target = C.stage(src, "inp_%s.png" % tag)
            bx = tuple(int(v * s) for v, s in zip(box, (W, H, W, H)))
            _mask_png((W, H), bx, feather).convert("RGB").save(
                os.path.join(C.IN_DIR, "inp_%s_mask.png" % tag))
            w, h = W - W % 8, H - H % 8
            mask_name = "inp_%s_mask.png" % tag
        graph = build(target, mask_name, w, h, job["prompt"], job.get("seed", 1), tag, steps)
        imgs = C.submit(graph)
        return C.pull(imgs, outdir, stem=tag) if outdir else []

    # ---- zoom redraw: crop with context -> upscale -> redraw inner box -> paste back
    x0, y0, x1, y1 = [int(v * s) for v, s in zip(box, (W, H, W, H))]
    bw, bh = x1 - x0, y1 - y0
    mg = int(float(job.get("context", 0.18)) * min(bw, bh))
    cx0, cy0 = max(0, x0 - mg), max(0, y0 - mg)
    cx1, cy1 = min(W, x1 + mg), min(H, y1 + mg)
    crop = im.crop((cx0, cy0, cx1, cy1))
    cw, ch = crop.size
    nw, nh = C.clamp_size(cw * zoom, ch * zoom)
    big = crop.resize((nw, nh), Image.LANCZOS)

    sx, sy = nw / float(cw), nh / float(ch)
    inner = (int((x0 - cx0) * sx), int((y0 - cy0) * sy),
             int((x1 - cx0) * sx), int((y1 - cy0) * sy))
    m = _mask_png((nw, nh), inner, int(feather * zoom))

    big.save(os.path.join(C.IN_DIR, "inp_%s_zoom.png" % tag))
    m.convert("RGB").save(os.path.join(C.IN_DIR, "inp_%s_zmask.png" % tag))

    graph = build("inp_%s_zoom.png" % tag, "inp_%s_zmask.png" % tag, nw, nh,
                  job["prompt"], job.get("seed", 1), "zoom_" + tag, steps)
    imgs = C.submit(graph)
    fixed = Image.open(C.out_path(imgs[0])).convert("RGB").resize((cw, ch), Image.LANCZOS)

    out = im.copy()
    out.paste(fixed, (cx0, cy0), m.resize((cw, ch), Image.LANCZOS))
    os.makedirs(outdir or ".", exist_ok=True)
    dst = os.path.join(outdir or ".", tag + "_zoomfix.jpg")
    out.save(dst, quality=96, subsampling=0)
    print("saved", dst)
    return [dst]


def main():
    ap = argparse.ArgumentParser(description="FLUX.2 Klein inpaint / zoom-fix")
    ap.add_argument("--in", dest="inp", required=True)
    ap.add_argument("--mask", help="整图重绘用的遮罩 PNG（与图同尺寸）")
    ap.add_argument("--box", help="重绘区域 x0,y0,x1,y1（相对 0-1）")
    ap.add_argument("--zoom", type=float, default=1.0, help=">1 时走裁切放大重绘")
    ap.add_argument("--context", type=float, default=0.18, help="裁切外扩比例")
    ap.add_argument("--feather", type=int, default=14, help="遮罩羽化半径")
    ap.add_argument("--prompt", required=True)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--steps", type=int, default=C.DEF["steps"])
    ap.add_argument("--out")
    ap.add_argument("--jobs")
    a = ap.parse_args()

    if a.jobs:
        for j in json.load(open(a.jobs, encoding="utf-8")):
            print(json.dumps(run_one(j, a.out), ensure_ascii=False))
        return
    box = [float(v) for v in a.box.split(",")] if a.box else None
    if a.zoom > 1.0 and not box:
        ap.error("--zoom 需要同时给 --box")
    job = {"in": a.inp, "mask": a.mask, "box": box, "zoom": a.zoom,
           "context": a.context, "feather": a.feather, "prompt": a.prompt,
           "seed": a.seed, "steps": a.steps}
    print(json.dumps(run_one(job, a.out), ensure_ascii=False))


if __name__ == "__main__":
    main()
