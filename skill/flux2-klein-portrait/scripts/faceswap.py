# -*- coding: utf-8 -*-
"""Identity / face swap with the Klein 9B swap LoRA (masked head region only).

image1 = target scene (with head mask), image2 = identity reference face.
The mask keeps body, clothing, hair silhouette and background untouched.

Example
-------
python faceswap.py --target shots/*.jpg --face ref_face.png --out ../out
python faceswap.py --target a.jpg --face ref.png --strength 0.88 --steps 4 --seed 9901
"""
import argparse
import glob
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import comfy as C

TRIGGER = C.C["lora"]["faceswap_trigger"]

DEFAULT_PROMPT = (
    "%s, transfer the face of the woman in Picture 2 onto the woman in Picture 1. "
    "Keep the pose, body, clothing, background, lighting and framing of Picture 1 exactly. "
    "Preserve her facial bone structure, eye shape, eye colour, eyebrows, nose and lips from Picture 2. "
    "Her forehead is completely bare and clean, with no crown, no headpiece, no jewellery, "
    "no forehead ornament; her hair is simply styled as in Picture 1. "
    "Keep the hairstyle and hair colour of Picture 1. "
    "Match the skin tone and shadow density of Picture 1, blend the neck and jawline naturally, "
    "photorealistic retouch, natural skin texture." % TRIGGER)


def build(target, mask, face, w, h, prompt, seed, prefix,
          steps=4, strength=0.95, cfg=1.0, lora_name=None, match=0.35):
    g = C.loaders()
    g["4"] = {"class_type": "LoraLoaderModelOnly", "inputs": {
        "model": ["1", 0], "lora_name": lora_name or C.C["lora"]["faceswap"],
        "strength_model": strength}}
    g["10"] = {"class_type": "LoadImage", "inputs": {"image": target}}
    g["11"] = {"class_type": "LoadImage", "inputs": {"image": mask}}
    g["12"] = {"class_type": "ImageToMask", "inputs": {"image": ["11", 0], "channel": "red"}}
    g["13"] = {"class_type": "LoadImage", "inputs": {"image": face}}
    g["20"] = {"class_type": "PainterFluxImageEdit", "inputs": {
        "clip": ["2", 0], "vae": ["3", 0], "prompt": prompt, "mode": "2_image",
        "batch_size": 1, "width": int(w), "height": int(h),
        "image1": ["10", 0], "image1_mask": ["12", 0], "image2": ["13", 0]}}
    g["30"] = {"class_type": "KSampler", "inputs": C.sampler("4", ("20", 0), ("20", 1), ("20", 2), seed, steps, cfg=cfg)}
    g["40"] = {"class_type": "VAEDecode", "inputs": {"samples": ["30", 0], "vae": ["3", 0]}}
    g["45"] = {"class_type": "ColorMatch", "inputs": {
        "image_ref": ["10", 0], "image_target": ["40", 0], "method": "mkl",
        "strength": match, "multithread": True}}
    g["50"] = {"class_type": "SaveImage", "inputs": {"images": ["45", 0], "filename_prefix": prefix}}
    return g


def main():
    ap = argparse.ArgumentParser(description="Klein 9B masked face swap")
    ap.add_argument("--target", nargs="+", required=True, help="目标图，支持通配符")
    ap.add_argument("--face", required=True, help="身份参考脸（建议 faceref.py 裁好）")
    ap.add_argument("--mask", help="统一的头部遮罩；省略则自动生成")
    ap.add_argument("--rx", type=float, default=0.80)
    ap.add_argument("--up", type=float, default=1.80)
    ap.add_argument("--down", type=float, default=1.30)
    ap.add_argument("--blur", type=int, default=22)
    ap.add_argument("--strength", type=float, default=0.95, help="LoRA 强度（0.88 更真实，0.95 更端正）")
    ap.add_argument("--steps", type=int, default=C.DEF["steps_lora"])
    ap.add_argument("--cfg", type=float, default=1.0)
    ap.add_argument("--seed", type=int, default=7001)
    ap.add_argument("--lora-name")
    ap.add_argument("--match", type=float, default=0.35, help="ColorMatch 强度")
    ap.add_argument("--prompt", default=DEFAULT_PROMPT)
    ap.add_argument("--out")
    ap.add_argument("--json-out")
    a = ap.parse_args()

    import headmask

    paths = []
    for p in a.target:
        paths.extend(sorted(glob.glob(p)) or [p])
    face = C.stage(a.face)
    C.require_server()

    results = []
    for i, p in enumerate(paths):
        if a.mask:
            from PIL import Image
            W, H = Image.open(p).size
            target, mask = C.stage(p, "tgt_%s.png" % os.path.splitext(os.path.basename(p))[0]), C.stage(a.mask)
        else:
            info = headmask.make_one(p, a.rx, a.up, a.down, a.blur)
            if info.get("error"):
                print("!! 跳过（未检测到人脸）：%s" % p, file=sys.stderr)
                results.append({"in": p, "error": info["error"]})
                continue
            W, H = info["size"]
            target, mask = info["target"], info["mask"]

        w, h = W - W % 8, H - H % 8
        prefix = os.path.splitext(os.path.basename(p))[0] + "_swap"
        graph = build(target, mask, face, w, h, a.prompt, a.seed + i, prefix,
                      a.steps, a.strength, a.cfg, a.lora_name, a.match)
        imgs = C.submit(graph)
        saved = C.pull(imgs, a.out, stem=prefix) if a.out else []
        results.append({"in": p, "out": saved})

    if a.json_out:
        json.dump(results, open(a.json_out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("DONE %d/%d" % (len([r for r in results if "out" in r]), len(paths)))


if __name__ == "__main__":
    main()
