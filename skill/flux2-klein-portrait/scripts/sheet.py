# -*- coding: utf-8 -*-
"""Contact sheets for review: full-frame grid or auto-cropped face grid.

Examples
--------
python sheet.py --in shots/*.jpg --out shots/_sheet.jpg --cols 5
python sheet.py --in shots/*.jpg --face-grid --out shots/_faces.jpg   # 换脸后逐张比对
"""
import argparse
import glob
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def collect(patterns):
    files = []
    for p in patterns:
        files.extend(sorted(glob.glob(p)) or [p])
    return files


def face_crop(path, size=240, margin=1.15):
    import cv2
    from PIL import Image
    im = Image.open(path).convert("RGB")
    W, H = im.size
    gray = cv2.cvtColor(cv2.imread(path), cv2.COLOR_BGR2GRAY)
    cascade = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    faces = cascade.detectMultiScale(gray, scaleFactor=1.06, minNeighbors=5, minSize=(24, 24))
    if len(faces):
        x, y, w, h = max(faces, key=lambda f: f[2] * f[3])
        cx, cy, s = x + w / 2.0, y + h / 2.0, max(w, h) * margin
    else:
        s = min(W, H) * 0.35
        cx, cy = W / 2.0, H * 0.28
    box = (int(cx - s / 2), int(cy - s / 2), int(cx + s / 2), int(cy + s / 2))
    return im.crop(box).resize((size, size), Image.LANCZOS)


def main():
    ap = argparse.ArgumentParser(description="contact sheet")
    ap.add_argument("--in", dest="inputs", nargs="+", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--cols", type=int, default=5)
    ap.add_argument("--thumb", type=int, default=320, help="单格宽度")
    ap.add_argument("--face-grid", action="store_true", help="裁脸拼图，用于检查身份一致性")
    ap.add_argument("--label", default=True, action="store_true")
    a = ap.parse_args()

    from PIL import Image, ImageDraw

    files = [f for f in collect(a.inputs) if os.path.isfile(f)]
    if not files:
        raise SystemExit("没有可用的图片")

    if a.face_grid:
        cols = a.cols if a.cols != 5 else 4
        tiles = [(os.path.basename(f), face_crop(f, a.thumb)) for f in files]
        rows = (len(tiles) + cols - 1) // cols
        sheet = Image.new("RGB", (cols * (a.thumb + 8), rows * (a.thumb + 30)), (240, 240, 240))
        d = ImageDraw.Draw(sheet)
        for i, (name, t) in enumerate(tiles):
            x = (i % cols) * (a.thumb + 8) + 4
            y = (i // cols) * (a.thumb + 30) + 26
            sheet.paste(t, (x, y))
            d.text((x, y - 20), name, fill=(0, 0, 0))
    else:
        ims = [Image.open(f).convert("RGB") for f in files]
        th = a.thumb
        heights = [max(1, int(im.height * th / float(im.width))) for im in ims]
        cell_h = max(heights)
        rows = (len(ims) + a.cols - 1) // a.cols
        sheet = Image.new("RGB", (a.cols * th, rows * (cell_h + 30)), (245, 245, 245))
        d = ImageDraw.Draw(sheet)
        for i, (f, im) in enumerate(zip(files, ims)):
            t = im.resize((th - 10, int(im.height * (th - 10) / float(im.width))), Image.LANCZOS)
            x = (i % a.cols) * th + (th - t.width) // 2
            y = (i // a.cols) * (cell_h + 30) + 28
            sheet.paste(t, (x, y))
            d.text(((i % a.cols) * th + 8, (i // a.cols) * (cell_h + 30) + 8),
                   os.path.basename(f), fill=(0, 0, 0))

    os.makedirs(os.path.dirname(os.path.abspath(a.out)) or ".", exist_ok=True)
    sheet.save(a.out, quality=92)
    print("saved %s %s" % (a.out, sheet.size))


if __name__ == "__main__":
    main()
