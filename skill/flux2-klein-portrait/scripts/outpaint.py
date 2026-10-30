# -*- coding: utf-8 -*-
"""Outpaint / canvas extension with ImagePadForOutpaint + ReferenceLatent.

The padded canvas becomes the new latent size, so width/height are derived from
the input size plus padding - never guessed.

Example
-------
python outpaint.py --in shot.jpg --pad 128,48,128,48 --prompt "..." --seed 7 --out ../out
python outpaint.py --jobs outpaint_jobs.json --out ../out
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import comfy as C


def build(img, pad, prompt, seed, prefix, steps=8, match=0.4):
    """pad = (left, top, right, bottom) in pixels."""
    from PIL import Image
    l, t, r, b = pad
    src = os.path.join(C.IN_DIR, img)
    w0, h0 = Image.open(src).size
    w, h = C.clamp_size(w0 + l + r, h0 + t + b)
    g = C.loaders()
    g["10"] = {"class_type": "LoadImage", "inputs": {"image": img}}
    g["20"] = {"class_type": "ImagePadForOutpaint", "inputs": {
        "image": ["10", 0], "left": l, "top": t, "right": r, "bottom": b, "feathering": 32}}
    g["25"] = {"class_type": "VAEEncode", "inputs": {"pixels": ["20", 0], "vae": ["3", 0]}}
    g["30"] = {"class_type": "CLIPTextEncode", "inputs": {"clip": ["2", 0], "text": prompt}}
    g["31"] = {"class_type": "ConditioningZeroOut", "inputs": {"conditioning": ["30", 0]}}
    g["40"] = {"class_type": "ReferenceLatent", "inputs": {"conditioning": ["30", 0], "latent": ["25", 0]}}
    g["41"] = {"class_type": "ReferenceLatent", "inputs": {"conditioning": ["31", 0], "latent": ["25", 0]}}
    g["50"] = {"class_type": "EmptyFlux2LatentImage", "inputs": {
        "width": w, "height": h, "batch_size": 1}}
    g["60"] = {"class_type": "KSampler", "inputs": C.sampler("1", ("40", 0), ("41", 0), "50", seed, steps)}
    g["70"] = {"class_type": "VAEDecode", "inputs": {"samples": ["60", 0], "vae": ["3", 0]}}
    g["80"] = {"class_type": "ColorMatch", "inputs": {
        "image_ref": ["20", 0], "image_target": ["70", 0], "method": "mkl",
        "strength": match, "multithread": True}}
    g["90"] = {"class_type": "SaveImage", "inputs": {"images": ["80", 0], "filename_prefix": prefix}}
    return g, (w, h)


def run_one(job, outdir):
    img = C.stage(job["in"])
    pad = job.get("pad", (128, 48, 128, 48))
    if isinstance(pad, str):
        pad = tuple(int(x) for x in pad.split(","))
    graph, size = build(img, pad, job["prompt"], job.get("seed", 1),
                        job.get("prefix", "outpaint"), job.get("steps", 8),
                        job.get("match", 0.4))
    imgs = C.submit(graph)
    print("canvas", size)
    return C.pull(imgs, outdir, stem=job.get("prefix")) if outdir else []


def main():
    ap = argparse.ArgumentParser(description="FLUX.2 Klein outpaint")
    ap.add_argument("--in", dest="inp")
    ap.add_argument("--pad", default="128,48,128,48", help="left,top,right,bottom 像素")
    ap.add_argument("--prompt")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--prefix", default="outpaint")
    ap.add_argument("--steps", type=int, default=C.DEF["steps"])
    ap.add_argument("--match", type=float, default=0.4, help="ColorMatch 强度，0 关闭")
    ap.add_argument("--out")
    ap.add_argument("--jobs")
    a = ap.parse_args()

    if a.jobs:
        for j in json.load(open(a.jobs, encoding="utf-8")):
            print(json.dumps(run_one(j, a.out), ensure_ascii=False))
        return
    if not (a.inp and a.prompt):
        ap.error("需要 --in 与 --prompt，或 --jobs")
    print(json.dumps(run_one({"in": a.inp, "pad": a.pad, "prompt": a.prompt, "seed": a.seed,
                              "prefix": a.prefix, "steps": a.steps, "match": a.match}, a.out),
                     ensure_ascii=False))


if __name__ == "__main__":
    main()
