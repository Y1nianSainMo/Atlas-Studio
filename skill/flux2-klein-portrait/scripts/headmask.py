# -*- coding: utf-8 -*-
"""Auto head mask for face swapping.

Detects the largest frontal face with a Haar cascade, expands it into a head
ellipse, blurs it, and stages both the target image and the mask into
ComfyUI/input so faceswap.py can use them directly.

Example
-------
python headmask.py --in shots/*.jpg --debug ../out/_maskcheck
"""
import argparse
import glob
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import comfy as C


def make_one(path, rx=0.80, up=1.80, down=1.30, blur=22, tag=None):
    import cv2
    from PIL import Image, ImageDraw, ImageFilter

    im = Image.open(path).convert("RGB")
    W, H = im.size
    gray = cv2.cvtColor(cv2.imread(path), cv2.COLOR_BGR2GRAY)
    cascade = cv2.CascadeClassifier(
        cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    faces = cascade.detectMultiScale(gray, scaleFactor=1.06, minNeighbors=5, minSize=(30, 30))
    if len(faces) == 0:
        return {"in": path, "error": "no face detected"}

    x, y, w, h = max(faces, key=lambda f: f[2] * f[3])
    cx, cy = x + w / 2.0, y + h / 2.0
    box = (max(0, int(cx - w * rx)), max(0, int(cy - h * up)),
           min(W, int(cx + w * rx)), min(H, int(cy + h * down)))

    m = Image.new("L", (W, H), 0)
    ImageDraw.Draw(m).ellipse(box, fill=255)
    m = m.filter(ImageFilter.GaussianBlur(blur))

    stem = tag or os.path.splitext(os.path.basename(path))[0]
    tgt = C.stage(path, "tgt_%s.png" % stem)
    mp = os.path.join(C.IN_DIR, "hsm_%s.png" % stem)
    m.convert("RGB").save(mp)

    dbg = im.copy()
    ImageDraw.Draw(dbg).rectangle([x, y, x + w, y + h], outline=(0, 255, 0), width=4)
    ImageDraw.Draw(dbg).ellipse(box, outline=(255, 60, 60), width=5)
    return {"in": path, "target": tgt, "mask": "hsm_%s.png" % stem,
            "face": [int(x), int(y), int(w), int(h)], "box": list(box),
            "size": [W, H], "debug": dbg}


def main():
    ap = argparse.ArgumentParser(description="auto head mask + staging")
    ap.add_argument("--in", dest="inputs", nargs="+", required=True,
                    help="图片路径，支持通配符")
    ap.add_argument("--rx", type=float, default=0.80, help="横向扩张系数（相对脸宽）")
    ap.add_argument("--up", type=float, default=1.80, help="向上扩张系数（相对脸高）")
    ap.add_argument("--down", type=float, default=1.30, help="向下扩张系数（相对脸高）")
    ap.add_argument("--blur", type=int, default=22)
    ap.add_argument("--debug", help="输出调试联络表的目录")
    ap.add_argument("--json-out", help="把结果写到这个 json 文件")
    a = ap.parse_args()

    paths = []
    for p in a.inputs:
        paths.extend(sorted(glob.glob(p)) or [p])

    rows = []
    for p in paths:
        r = make_one(p, a.rx, a.up, a.down, a.blur)
        print(json.dumps({k: v for k, v in r.items() if k != "debug"}, ensure_ascii=False))
        rows.append(r)

    if a.debug:
        from PIL import Image, ImageDraw
        os.makedirs(a.debug, exist_ok=True)
        ok = [r for r in rows if "debug" in r]
        if ok:
            cols, cw, ch = min(5, len(ok)), 300, 520
            rows_n = (len(ok) + cols - 1) // cols
            sheet = Image.new("RGB", (cols * cw, rows_n * ch), (245, 245, 245))
            d = ImageDraw.Draw(sheet)
            for i, r in enumerate(ok):
                t = r["debug"].copy()
                t.thumbnail((cw - 12, ch - 34))
                sheet.paste(t, ((i % cols) * cw + (cw - t.width) // 2, (i // cols) * ch + 30))
                d.text(((i % cols) * cw + 10, (i // cols) * ch + 10),
                       os.path.basename(r["in"]), fill=(0, 0, 0))
            out = os.path.join(a.debug, "headmask_check.jpg")
            sheet.save(out, quality=90)
            print("debug sheet ->", out)

    if a.json_out:
        json.dump([{k: v for k, v in r.items() if k != "debug"} for r in rows],
                  open(a.json_out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    bad = [r["in"] for r in rows if r.get("error")]
    if bad:
        print("!! 未检测到人脸，需要手写遮罩：", bad, file=sys.stderr)


if __name__ == "__main__":
    main()
