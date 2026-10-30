# -*- coding: utf-8 -*-
"""Crop an identity reference face from a portrait.

The swap LoRA reads image2 as identity. A tight, well-lit face crop transfers
identity far better than the full photo, and it prevents props (crowns, veils,
head chains, jewellery) from being copied onto the target.

Example
-------
python faceref.py --in desert.jpg --out ref_face.png
python faceref.py --in desert.jpg --out ref_face.png --top 0.15   # 避开额前饰品
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import comfy as C


def crop_face(src, dst, sides=0.18, top=0.06, bottom=0.14, scale=2, from_eyes=None):
    import cv2
    from PIL import Image

    im = Image.open(src).convert("RGB")
    W, H = im.size
    gray = cv2.cvtColor(cv2.imread(src), cv2.COLOR_BGR2GRAY)
    cascade = cv2.CascadeClassifier(
        cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    faces = cascade.detectMultiScale(gray, scaleFactor=1.06, minNeighbors=5, minSize=(30, 30))
    if len(faces) == 0:
        raise SystemExit("未检测到人脸：%s\n用 --manual x0,y0,x1,y1 手动指定" % src)
    x, y, w, h = max(faces, key=lambda f: f[2] * f[3])
    cx = x + w / 2.0
    top_y = y + h * from_eyes if from_eyes is not None else y - h * top
    box = (max(0, int(cx - w * (0.5 + sides))),
           max(0, int(top_y)),
           min(W, int(cx + w * (0.5 + sides))),
           min(H, int(y + h * (1 + bottom))))
    return crop_box(src, dst, box, scale)


def crop_box(src, dst, box, scale=2):
    from PIL import Image
    im = Image.open(src).convert("RGB")
    c = im.crop(box)
    if scale != 1:
        c = c.resize((c.width * scale, c.height * scale), Image.LANCZOS)
    os.makedirs(os.path.dirname(os.path.abspath(dst)) or ".", exist_ok=True)
    c.save(dst)
    print("saved %s %s box=%s" % (dst, c.size, box))
    return dst


def main():
    ap = argparse.ArgumentParser(description="crop identity reference face")
    ap.add_argument("--in", dest="inp", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--sides", type=float, default=0.18)
    ap.add_argument("--top", type=float, default=0.06, help="向上留白（可能带进额饰）")
    ap.add_argument("--from-eyes", type=float, default=None,
                    help="从检测框顶部往下 0~1 处开始裁，例如 0.26 可避开额前王冠/额链")
    ap.add_argument("--bottom", type=float, default=0.14)
    ap.add_argument("--scale", type=int, default=2)
    ap.add_argument("--manual", help="x0,y0,x1,y1 绝对像素，跳过人脸检测")
    ap.add_argument("--stage", action="store_true", help="同时拷进 ComfyUI/input")
    a = ap.parse_args()

    if a.manual:
        out = crop_box(a.inp, a.out, tuple(int(v) for v in a.manual.split(",")), a.scale)
    else:
        out = crop_face(a.inp, a.out, a.sides, a.top, a.bottom, a.scale, a.from_eyes)
    if a.stage:
        print("staged as", C.stage(out))


if __name__ == "__main__":
    main()
