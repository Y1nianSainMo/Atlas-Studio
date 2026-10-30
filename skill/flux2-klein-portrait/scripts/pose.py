# -*- coding: utf-8 -*-
"""RuiC-PoseAtlas bridge - pose reference library for flux2-klein-portrait.

RuiC-PoseAtlas (https://github.com/HRuiCcc/RuiC-PoseAtlas) is a static gallery of
288 body poses x 4-5 camera angles, drawn as grey line-art mannequins on a white
background, 900x1200 PNG.  Fed as Picture 1 to PainterFluxImageEdit it acts like a
pose ControlNet: the model keeps the limb positions / camera framing but renders a
photograph.  Paired with an identity photo as Picture 2 you get "same person, new
pose" without hand-writing pose descriptions.

This module keeps a small local mirror (data + the few images you actually use)
so nothing but the demo site is needed - no 654 MB clone.

Subcommands
-----------
  index   下载 / 刷新 poses.json 索引到 <skill>/pose_atlas/
  search  按关键词 / 系列 / 分类 / 角度筛选，可出缩略图联络表
  get     把某个姿态某个角度取到本地（可选缩放、可 stage 进 ComfyUI input）
  jobs    批量生成 gen.py 的 jobs.json（自动带上姿态图 + 可选身份图）

Examples
--------
python pose.py search --query 站姿 --series full_std --limit 6 --sheet pick.jpg
python pose.py get --id full_sexy_P001 --angle front --out ref/p001.png
python pose.py jobs --query 坐姿 --limit 4 --identity me.jpg --out jobs.json
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import comfy as C

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ATLAS = C.C.get("pose_atlas", {})
BASE = (os.environ.get("POSE_ATLAS_BASE") or ATLAS.get(
    "base") or "https://hruiccc.github.io/RuiC-PoseAtlas").rstrip("/")
CACHE = ATLAS.get("cache") or os.path.join(ROOT, "pose_atlas")
INDEX = os.path.join(CACHE, "poses.json")
STALE_DAYS = 30

ANGLE_ALIAS = {
    "front": "front", "正": "front", "正面": "front",
    "side": "side", "侧": "side", "侧面": "side",
    "diagonal": "diagonal", "diag": "diagonal", "斜": "diagonal", "斜侧": "diagonal",
    "high": "high", "俯": "high", "俯视": "high",
    "low": "low", "仰": "low", "仰视": "low",
}


# ------------------------------------------------------------------ download io
def _get(url, timeout=45, retries=4):
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "flux2-klein-portrait/1.0"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except Exception as e:                                    # noqa: BLE001
            last = e
            time.sleep(1.2 * (i + 1))
    raise SystemExit("下载失败 %s\n  %s" % (url, last))


REMOTE = "https://hruiccc.github.io/RuiC-PoseAtlas"


def base_dir():
    """本地 clone 优先；clone 不在了自动回落到线上演示站。"""
    if BASE.startswith(("http://", "https://", "file://")):
        return BASE
    if os.path.exists(os.path.join(BASE, "data", "poses.json")):
        return BASE
    return REMOTE


def _url(rel):
    b = base_dir()
    if b.startswith(("http://", "https://", "file://")):
        return b + "/" + rel
    return os.path.join(b, rel)


def _is_local(url):
    return not url.startswith(("http://", "https://"))


def _download(url, dst):
    os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
    if os.path.exists(dst) and os.path.getsize(dst) > 1024:
        return dst
    if _is_local(url):
        src = url[7:] if url.startswith("file://") else url
        if not os.path.exists(src):
            raise SystemExit("本地文件不存在 %s" % src)
        import shutil
        shutil.copy2(src, dst)
        return dst
    blob = _get(url)
    if len(blob) < 1024:
        raise SystemExit("下载内容异常（%d 字节）%s" % (len(blob), url))
    tmp = dst + ".part"
    with open(tmp, "wb") as f:
        f.write(blob)
    os.replace(tmp, dst)
    return dst


def load_index(force=False):
    """Local mirror of data/poses.json, refreshed when missing or stale."""
    fresh = (os.path.exists(INDEX) and
             time.time() - os.path.getmtime(INDEX) < STALE_DAYS * 86400)
    if force or not fresh:
        url = _url("data/poses.json")
        try:
            if _is_local(url):
                with open(url, "rb") as f:
                    blob = f.read()
            else:
                blob = _get(url)
            data = json.loads(blob.decode("utf-8"))
            os.makedirs(CACHE, exist_ok=True)
            with open(INDEX, "wb") as f:
                f.write(blob)
            print("[index] 已更新 %s (%d 姿态)" % (INDEX, len(data["poses"])), flush=True)
            return data
        except SystemExit:
            if not os.path.exists(INDEX):
                raise
            print("[index] 在线更新失败，沿用本地缓存", flush=True)
    with open(INDEX, encoding="utf-8") as f:
        return json.load(f)


# ------------------------------------------------------------------- selection
def _names(p):
    n = p.get("names") or {}
    return " ".join(str(n.get(k, "")) for k in ("zh", "en", "ja"))


def _angle_ok(variant, want):
    if not want or want == "all":
        return True
    key = ANGLE_ALIAS.get(str(want).lower(), str(want))
    return variant["angle"] == key


def select(data, query=None, series=None, category=None, angle=None,
           ids=None, limit=0):
    pool = data["poses"]
    if ids:
        full_ids, nos = set(), set()
        for w in ids.replace(" ", ",").split(","):
            w = w.strip()
            if not w:
                continue
            (full_ids if "_" in w else nos).add(w.upper() if "_" not in w else w)
        pool = [p for p in pool if p["id"] in full_ids or p["no"].upper() in nos]
        want = {k: i for i, k in enumerate(
            [x.strip() for x in ids.replace(" ", ",").split(",") if x.strip()])}
        pool.sort(key=lambda p: want.get(p["id"], want.get(p["no"].upper(), 999)))
    if series:
        keys = {s.strip() for s in series.split(",")}
        pool = [p for p in pool if p["series"] in keys]
    if category:
        cats = {c.strip() for c in category.split(",")}
        pool = [p for p in pool if p["category"] in cats]
    if query:
        terms = [t for t in str(query).replace("，", " ").split() if t]
        scored = []
        for p in pool:
            hay = (_names(p) + " " + p["id"] + " " + p["no"] + " " + p["category"]).lower()
            hits = sum(1 for t in terms if t.lower() in hay)
            if hits:
                scored.append((hits, p))
        scored.sort(key=lambda x: -x[0])
        pool = [p for _, p in scored]
    pool = [p for p in pool if any(_angle_ok(v, angle) for v in p["variants"])]
    return pool[:limit] if limit else pool


def pick_variant(pose, angle="front"):
    """Return the variant dict for the requested angle, else the first available."""
    if angle and angle != "all":
        key = ANGLE_ALIAS.get(str(angle).lower(), str(angle))
        for v in pose["variants"]:
            if v["angle"] == key:
                return v
        raise SystemExit("%s 没有 %s 角度，可选：%s" % (
            pose["id"], key, [v["angle"] for v in pose["variants"]]))
    return pose["variants"][0]


# ----------------------------------------------------------------------- fetch
def fetch(pose, angle="front", kind="full", out=None, size=None):
    """Download one pose variant locally; returns the file path."""
    v = pick_variant(pose, angle)
    rel = v["full"] if kind == "full" else v["thumb"]
    src = _url(rel)
    # 本地 clone 场景直接用原文件，不复制进缓存
    if (out is None and not size or str(size).lower() == "auto") and _is_local(src) \
            and os.path.exists(src):
        return src
    ext = os.path.splitext(rel)[1]
    dst = out or os.path.join(CACHE, "img", pose["id"] + "_" + v["angle"] + ext)
    if not os.path.isabs(dst):
        dst = os.path.join(os.getcwd(), dst)
    _download(_url(rel), dst)
    if size and str(size).lower() != "auto":
        from PIL import Image
        im = _fit(Image.open(dst).convert("RGB"), *C.parse_size(size))
        dst = os.path.splitext(dst)[0] + "_%s.png" % str(size).replace("x", "x")
        im.save(dst)
    return dst


def _fit(im, tw, th):
    """Letterbox into exactly tw x th without cropping the figure."""
    tw, th = C.clamp_size(tw, th)
    w, h = im.size
    k = min(tw / float(w), th / float(h))
    im = im.resize((max(1, int(w * k)), max(1, int(h * k))), 1)
    from PIL import Image
    canvas = Image.new("RGB", (tw, th), (255, 255, 255))
    canvas.paste(im, ((tw - im.width) // 2, (th - im.height) // 2))
    return canvas


def auto_size(pose, angle, long_side=1024):
    """Match the atlas 3:4 frame by default so the framing survives the reference."""
    from PIL import Image
    w, h = Image.open(fetch(pose, angle, "full")).size
    k = long_side / float(max(w, h))
    return C.clamp_size(int(w * k), int(h * k))


# ------------------------------------------------------------------ prompt glue
PROMPT_WITH_IDENTITY = (
    "Photorealistic full-body studio photograph. "
    "Picture 1 is a grey line-art mannequin drawing that ONLY defines the body pose, "
    "limb positions and camera framing - ignore its drawing style completely. "
    "Recreate that exact pose: {pose_en}. "
    "Picture 2 defines the woman's face, hair, body type and outfit - put that woman "
    "into the pose of Picture 1, keeping her face and clothes identical. "
    "Real skin texture, natural anatomy, exactly two legs and two feet, "
    "seamless plain studio backdrop, soft even lighting, sharp focus."
)

PROMPT_NO_IDENTITY = (
    "Photorealistic full-body studio photograph of an adult woman. "
    "Picture 1 is a grey line-art mannequin drawing that ONLY defines the body pose, "
    "limb positions and camera framing - ignore its drawing style completely. "
    "Recreate that exact pose: {pose_en}. "
    "Real skin texture, natural anatomy, exactly two legs and two feet, "
    "seamless plain light-grey studio backdrop, soft even lighting, sharp focus."
)


def render(template, pose, variant, data):
    angles = {a["key"]: a["names"] for a in data.get("angles", [])}
    an = angles.get(variant["angle"], {})
    return (template
            .replace("{pose}", pose["names"].get("zh", ""))
            .replace("{pose_en}", pose["names"].get("en", ""))
            .replace("{pose_ja}", pose["names"].get("ja", ""))
            .replace("{id}", pose["id"]).replace("{no}", pose["no"])
            .replace("{category}", pose.get("category", ""))
            .replace("{angle}", variant["angle"])
            .replace("{angle_zh}", an.get("zh", variant["angle"]))
            .replace("{angle_en}", an.get("en", variant["angle"])))


# ------------------------------------------------------------------ subcommands
def cmd_search(a):
    data = load_index(a.refresh)
    pool = select(data, a.query, a.series, a.category, a.angle, a.ids, a.limit)
    if not pool:
        print("没有匹配的姿态。可用系列：%s" % [
            s["key"] for s in data["series"]])
        return
    for p in pool:
        angs = ",".join(v["angle"] for v in p["variants"])
        print("%-22s %-6s %-16s %-24s [%s]" % (
            p["id"], p["no"], p["category"], p["names"].get("zh", "")[:22], angs))
    print("-- %d 条（共 %d 姿态 / %d 图）" % (len(pool), data["totals"]["poses"],
                                          data["totals"]["images"]))
    if a.sheet:
        make_sheet(pool, a.angle, a.sheet, per_row=a.cols)


def make_sheet(pool, angle, dst, per_row=6):
    from PIL import Image, ImageDraw
    tiles = []
    for p in pool:
        v = pick_variant(p, angle)
        t = os.path.join(CACHE, "thumb", os.path.basename(v["thumb"]))
        try:
            _download(_url(v["thumb"]), t)
        except SystemExit as e:
            print("跳过 %s: %s" % (p["id"], e))
            continue
        tiles.append((p, v, Image.open(t).convert("RGB")))
    if not tiles:
        return
    cw, ch = 180, 264
    rows = (len(tiles) + per_row - 1) // per_row
    sheet = Image.new("RGB", (per_row * cw, rows * ch), (248, 248, 248))
    d = ImageDraw.Draw(sheet)
    for i, (p, v, im) in enumerate(tiles):
        im = im.resize((cw - 12, ch - 40), 1)
        x, y = (i % per_row) * cw + 6, (i // per_row) * ch + 30
        sheet.paste(im, (x, y))
        d.text((x, y - 22), "%s %s" % (p["no"], v["angle"]), fill=(20, 20, 20))
    os.makedirs(os.path.dirname(os.path.abspath(dst)), exist_ok=True)
    sheet.save(dst, quality=92)
    print("[sheet] %s (%d 格)" % (dst, len(tiles)))


def cmd_get(a):
    data = load_index(a.refresh)
    pool = select(data, a.query, a.series, a.category, a.angle, a.ids, 1)
    if not pool:
        raise SystemExit("没有匹配的姿态")
    pose = pool[0]
    v = pick_variant(pose, a.angle)
    dst = fetch(pose, a.angle, a.kind, a.out, a.size)
    print(json.dumps({"id": pose["id"], "angle": v["angle"], "file": dst,
                      "name": pose["names"].get("zh", "")}, ensure_ascii=False))
    if a.stage:
        print("[stage] %s" % C.stage(dst))


def cmd_jobs(a):
    data = load_index(a.refresh)
    angles = [x.strip() for x in (a.angle or "front").split(",")]
    want_all = angles == ["all"]
    pool = select(data, a.query, a.series, a.category,
                  None if (want_all or len(angles) > 1) else angles[0],
                  a.ids, a.limit)
    if not pool:
        raise SystemExit("没有匹配的姿态")
    tpl = a.template or (PROMPT_WITH_IDENTITY if a.identity else PROMPT_NO_IDENTITY)
    if a.prompt:
        with open(a.prompt, encoding="utf-8") as f:
            tpl = f.read()
    jobs, i = [], 0
    for p in pool:
        vs = p["variants"] if want_all else [pick_variant(p, x) for x in angles]
        for v in vs:
            i += 1
            ref_dir = a.ref_dir or os.path.join(os.getcwd(), "pose_refs")
            ref = fetch(p, v["angle"], "full", os.path.join(ref_dir,
                                                           "%s_%s.png" % (p["id"], v["angle"])))
            refs = [ref] + ([os.path.abspath(a.identity)] if a.identity else [])
            size = a.size
            if not size or str(size).lower() == "auto":
                size = "%dx%d" % auto_size(p, v["angle"], a.long_side)
            jobs.append({
                "prompt": render(tpl, p, v, data),
                "refs": refs,
                "size": size,
                "seed": a.seed + i if a.seed else 1000 + i,
                "prefix": "%s_%s" % (p["no"], v["angle"]),
            })
    out = a.out or "jobs.json"
    with open(out, "w", encoding="utf-8") as f:
        json.dump(jobs, f, ensure_ascii=False, indent=1)
    print("[jobs] %s -> %d 个任务（%d 姿态 x 角度）" % (out, len(jobs), len(pool)))
    print("下一步: python gen.py --jobs %s --out <成品目录>" % out)


def main():
    ap = argparse.ArgumentParser(description="RuiC-PoseAtlas 姿态参考桥")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p):
        p.add_argument("--query", "-q", help="关键词（中/英/日/编号）")
        p.add_argument("--series", help="full_sexy,full_std,upper,duo")
        p.add_argument("--category", help="站姿,坐姿,跪立...")
        p.add_argument("--angle", default="front", help="front/side/diagonal/high/low/all")
        p.add_argument("--ids", help="逗号分隔的姿态 id 或编号，如 full_std_P003,P010")
        p.add_argument("--refresh", action="store_true", help="强制刷新索引")

    p = sub.add_parser("index", help="下载/刷新索引")
    p.add_argument("--refresh", action="store_true")
    p.set_defaults(func=lambda a: print(
        "[index] %d 姿态 / %d 图 -> %s" % (
            (d := load_index(True))["totals"]["poses"], d["totals"]["images"], INDEX)))

    p = sub.add_parser("search", help="筛选姿态")
    common(p)
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--sheet", help="输出缩略图联络表到该路径")
    p.add_argument("--cols", type=int, default=6)
    p.set_defaults(func=cmd_search)

    p = sub.add_parser("get", help="取单张姿态图")
    common(p)
    p.add_argument("--limit", type=int, default=1)
    p.add_argument("--kind", choices=["full", "thumb"], default="full")
    p.add_argument("--size", help="WxH，等比留白贴到该尺寸")
    p.add_argument("--out", help="保存路径")
    p.add_argument("--stage", action="store_true", help="同时复制进 ComfyUI/input")
    p.set_defaults(func=cmd_get)

    p = sub.add_parser("jobs", help="批量生成 gen.py 的任务文件")
    common(p)
    p.add_argument("--limit", type=int, default=9)
    p.add_argument("--identity", help="身份参考照（作为 Picture 2）")
    p.add_argument("--prompt", help="提示词模板文件（含 {pose_en} 等占位符）")
    p.add_argument("--template", help="直接给模板字符串")
    p.add_argument("--size", default="auto", help="auto 或 WxH")
    p.add_argument("--long-side", type=int, default=1024)
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--ref-dir", help="姿态图落盘目录，默认 ./pose_refs")
    p.add_argument("--out", default="jobs.json")
    p.set_defaults(func=cmd_jobs)

    a = ap.parse_args()
    a.func(a)


if __name__ == "__main__":
    main()
