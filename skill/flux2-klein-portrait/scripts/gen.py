# -*- coding: utf-8 -*-
"""Text-to-image and reference-guided generation with FLUX.2 Klein.

mode 1_image  -> pure text-to-image when no --ref is given
mode N_image  -> N reference images (up to 3 commonly used, node supports 8)

Examples
--------
python gen.py --prompt "studio portrait of a woman ..." --size 864x1536 --seed 21 --out ../out
python gen.py --prompt "same girl, same outfit, new pose ..." --ref a.jpg --ref b.jpg --seed 22 --out ../out
python gen.py --jobs jobs.json --out ../out
"""
import argparse
import json
import time
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import comfy as C


def build(prompt, width, height, seed, steps=8, refs=(), prefix="gen", batch=1):
    g = C.loaders()
    width, height = C.clamp_size(width, height)
    mode = "%d_image" % max(1, len(refs))
    inputs = {"clip": ["2", 0], "vae": ["3", 0], "prompt": prompt, "mode": mode,
              "batch_size": int(batch), "width": int(width), "height": int(height)}
    for i, ref in enumerate(refs, 1):
        nid = str(10 + i)
        g[nid] = {"class_type": "LoadImage", "inputs": {"image": ref}}
        inputs["image%d" % i] = [nid, 0]

    g["20"] = {"class_type": "PainterFluxImageEdit", "inputs": inputs}
    g["30"] = {"class_type": "KSampler", "inputs": C.sampler("1", ("20", 0), ("20", 1), ("20", 2), seed, steps)}
    g["40"] = {"class_type": "VAEDecode", "inputs": {"samples": ["30", 0], "vae": ["3", 0]}}
    g["50"] = {"class_type": "SaveImage", "inputs": {"images": ["40", 0], "filename_prefix": prefix}}
    return g


def run_job(job, outdir, verbose=True):
    refs = [C.stage(r) for r in job.get("refs", []) if r]
    size = job.get("size") or "864x1536"
    w, h = size if isinstance(size, (list, tuple)) else C.parse_size(size)
    prefix = job.get("prefix") or "gen_%s" % time.strftime("%H%M%S")
    graph = build(job["prompt"], w, h, job.get("seed", 1), job.get("steps", 8),
                  refs, prefix, job.get("batch", 1))
    imgs = C.submit(graph, verbose=verbose)
    saved = C.pull(imgs, outdir, stem=prefix) if outdir else []
    return saved


def main():
    ap = argparse.ArgumentParser(description="FLUX.2 Klein text/image-to-image")
    ap.add_argument("--prompt")
    ap.add_argument("--ref", action="append", default=[], help="参考图，可重复最多 8 次")
    ap.add_argument("--size", default="864x1536", help="WxH，默认 864x1536")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--prefix", help="文件名前缀，省略则按时间生成")
    ap.add_argument("--steps", type=int, default=C.DEF["steps"])
    ap.add_argument("--batch", type=int, default=1)
    ap.add_argument("--out", help="成品目录，省略则只留在 ComfyUI/output")
    ap.add_argument("--jobs", help="批量任务 JSON")
    a = ap.parse_args()

    if a.jobs:
        jobs = json.load(open(a.jobs, encoding="utf-8"))
        for j in jobs:
            print(json.dumps(run_job(j, a.out), ensure_ascii=False))
        return

    if not a.prompt:
        ap.error("需要 --prompt 或 --jobs")
    print(json.dumps(run_job({
        "prompt": a.prompt, "refs": a.ref, "size": a.size, "seed": a.seed,
        "prefix": a.prefix, "steps": a.steps, "batch": a.batch}, a.out), ensure_ascii=False))


if __name__ == "__main__":
    main()
