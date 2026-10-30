# -*- coding: utf-8 -*-
"""Atlas Studio - 姿态库 + 衣装库 拖放生图台。

一个只用标准库的本地服务：
  * 提供两个素材库的检索接口（RuiC-PoseAtlas / COSTUME-DB-01 本地镜像）
  * 接收拖入/上传的参考图，拼 comfy 节点图提交给本机 ComfyUI
  * 轮询进度、取回成品，可选再跑一遍换脸 LoRA 锁脸

启动：
  E:/ComfyUI_Mie_V33/python_embeded/python.exe E:/atlas-studio/studio_server.py
  浏览器打开 http://127.0.0.1:8125/
"""
import json
import os
import re
import shutil
import sys
import threading
import time
import traceback
import urllib.parse
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
CFG_PATH = os.path.join(HERE, "config.json")
with open(CFG_PATH, encoding="utf-8") as f:
    CFG = json.load(f)

PORT = int(CFG.get("port", 8125))
SKILL = CFG["skill_dir"]
sys.path.insert(0, os.path.join(SKILL, "scripts"))
import comfy as C            # noqa: E402  技能里的节点图零件
import faceswap as FS        # noqa: E402
import headmask as HM        # noqa: E402
import faceref as FR         # noqa: E402

WORK = os.path.join(HERE, "work")
UP = os.path.join(WORK, "uploads")
OUT = os.path.join(WORK, "outputs")          # 输出根目录，下面按日期分文件夹
OUTROOT = OUT
TMP = os.path.join(WORK, "tmp")              # 中间产物（人脸裁剪/遮罩/原图），别混进出图目录
DEPTH_CACHE = os.path.join(WORK, "depth")
SKEL_CACHE = os.path.join(WORK, "pose")
for _d in (OUT, TMP, DEPTH_CACHE, SKEL_CACHE):
    os.makedirs(_d, exist_ok=True)


def out_dir():
    """按日期取输出文件夹：work/outputs/2026-10-29/"""
    d = os.path.join(OUTROOT, time.strftime("%Y-%m-%d"))
    os.makedirs(d, exist_ok=True)
    return d


def out_stem():
    """输出文件主名：21-33-12。同一秒内重复就补 -2、-3。"""
    d = out_dir()
    base = time.strftime("%H-%M-%S")
    stem, n = base, 1
    while os.path.exists(os.path.join(d, stem + "_成品.jpg")):
        n += 1
        stem = "%s-%d" % (base, n)
    return stem
for d in (UP, OUT):
    os.makedirs(d, exist_ok=True)

POSE_DIR = CFG["pose_dir"]
COSTUME_DIR = CFG["costume_dir"]

# ------------------------------------------------------------------ 素材索引
_lock = threading.Lock()
_poses = {"data": None, "mtime": 0}
_costs = {"data": None, "mtime": 0}


def poses():
    p = os.path.join(POSE_DIR, "data", "poses.json")
    mt = os.path.getmtime(p)
    with _lock:
        if _poses["data"] is None or mt != _poses["mtime"]:
            with open(p, encoding="utf-8") as f:
                _poses["data"] = json.load(f)
            _poses["mtime"] = mt
        return _poses["data"]


def costumes():
    p = os.path.join(COSTUME_DIR, "index.html")
    mt = os.path.getmtime(p)
    with _lock:
        if _costs["data"] is None or mt != _costs["mtime"]:
            html = open(p, encoding="utf-8").read()
            m = re.search(r"const\s+D\s*=\s*(\[.*?\])\s*;", html, re.S)
            if m:
                _costs["data"] = json.loads(m.group(1))
            else:                                    # 退化：按条目正则抽
                items = []
                for mm in re.finditer(
                        r'\{"id": "([^"]+)", "カテゴリ": "([^"]*)", "キーワード": "([^"]*)", '
                        r'"サムネ": "([^"]*)", "画像": "([^"]*)", "原寸": "([^"]*)"\}', html):
                    items.append({"id": mm.group(1), "カテゴリ": mm.group(2),
                                  "キーワード": mm.group(3), "サムネ": mm.group(4),
                                  "画像": mm.group(5), "原寸": mm.group(6)})
                _costs["data"] = items
            _costs["mtime"] = mt
        return _costs["data"]


ANGLE_ALIAS = {"front": "front", "正面": "front", "side": "side", "侧面": "side",
               "diagonal": "diagonal", "diag": "diagonal", "斜侧": "diagonal",
               "high": "high", "俯视": "high", "low": "low", "仰视": "low"}


def find_pose(pid, angle="front"):
    for p in poses()["poses"]:
        if p["id"] == pid:
            a = ANGLE_ALIAS.get(str(angle).lower(), str(angle))
            for v in p["variants"]:
                if v["angle"] == a:
                    return p, v
            return p, p["variants"][0]
    raise KeyError(pid)


def find_costume(cid):
    for c in costumes():
        if c["id"] == cid:
            return c
    raise KeyError(cid)


# --------------------------------------------------------------- 提示词拼装
BASE_TAIL = ("A real human woman with natural skin, real hair and real eyes is the subject. "
             "Photorealistic full-body studio photograph, seamless plain light studio backdrop, "
             "soft even studio lighting, sharp focus, real skin texture, "
             "anatomically correct body, exactly two legs and two feet, "
             "the legs clearly separated as one left leg and one right leg, "
             "each leg ends in a single foot wearing a single shoe.")

# 腿部结构容易崩（交叉腿/并拢腿常画出第三条腿），单独一条负向压住
NO_EXTRA_LIMBS = ("No third leg, no extra leg, no duplicated limb, no extra foot, no three feet, "
                  "no fused or merged legs, no ghost leg, no extra arm, no extra hand, "
                  "no mutated or conjoined anatomy.")

# 体型预设：不写身材时模型会给"平均体型"，在棚拍全身图里显壮。
# 每个档位都配一句负向，压住反方向的体型。
BODY_PRESETS = {
    "ultra": ("extremely skinny body with almost no body fat, very thin waist and narrow hips, "
              "completely flat stomach, pronounced thigh gap, very thin slender arms and legs, "
              "visible collarbones and shoulder blades, small delicate frame, "
              "runway editorial skinny silhouette, no curves at all"),
    "xxthin": ("very thin slim figure, narrow waist, completely flat stomach, clear thigh gap, "
               "thin slender legs and arms, visible collarbones, delicate shoulders, "
               "small light frame, very slim idol body proportions, no body fat"),
    "slim": ("slim slender figure, narrow waist, flat stomach, slim thighs with thigh gap, "
             "long slender legs, delicate shoulders, light frame, "
             "slim k-pop idol proportions, no extra weight anywhere"),
    "model": ("tall high-fashion model proportions, very long slim legs, slim toned body, "
              "small waist, narrow elegant silhouette, willowy slender frame"),
    "normal": ("lean healthy figure on the slim side, balanced proportions, fit and slim, "
               "neither thin nor heavy, no fullness"),
    "curvy": ("curvy figure with a clearly narrow waist, fuller bust and hips, flat stomach, "
              "soft curves without any heaviness, slim-waisted hourglass"),
    "plus": ("fuller plus-size figure, soft rounded body, wide hips, full thighs, "
             "generous curves, firm and well proportioned"),
}
BODY_NEG = {
    "ultra": "No body fat, no plump or chubby body, no thick waist, no heavy thighs, "
             "no wide hips, no curves, no muscular bulk, no stocky build.",
    "xxthin": "No overweight, no plump or chubby body, no thick waist, no heavy thighs, "
              "no wide hips, no stocky build, no bulky frame, no belly.",
    "slim": "No overweight, no plump or chubby body, no thick waist, no heavy thighs, "
            "no wide hips, no stocky build, no bulky frame, no belly.",
    "model": "No overweight, no plump or chubby body, no thick waist, no short legs, "
             "no stocky build, no bulky frame.",
    "normal": "No obesity, no fullness, no thick waist, no oversized hips, no emaciated body.",
    "curvy": "No flat boyish figure, no heavy body, no thick waist, no emaciated frame.",
    "plus": "No extremely thin body, no emaciated figure, no bony frame, no underweight look.",
}
BODY_ALIAS = {
    "ultra": "ultra", "超瘦": "ultra", "纸片": "ultra", "骨感": "ultra", "极瘦": "ultra",
    "xxthin": "xxthin", "thin": "xxthin", "skinny": "xxthin",
    "slim": "slim", "细": "slim", "瘦": "slim", "纤细": "slim",
    "model": "model", "模特": "model",
    "normal": "normal", "标准": "normal", "匀称": "normal",
    "curvy": "curvy", "丰满": "curvy", "曲线": "curvy",
    "plus": "plus", "胖": "plus", "丰腴": "plus", "偏胖": "plus",
}
DEFAULT_BODY = "xxthin"
# 参考图横向压缩比：提示词对体型的控制已经到顶（cfg=1 负向无效，正向词再多也拉不动），
# 真正有效的是让模型"看到"的人本身就是窄的 —— 把人横向压窄、画布尺寸不变。
# 实测 0.90 明显瘦一档，0.82 更极端，姿势仍能还原。
BODY_WARP = {
    "ultra": 0.76,
    "xxthin": 0.84,
    "slim": 0.89,
    "model": 0.88,
    "normal": 0.95,
    "curvy": 1.00,
    "plus": 1.05,
}
# 体型在提示词里出现两次：紧跟用户描述（权重最高的一段）＋结尾再强调一次。
# 之所以必须重复，是因为本模型 cfg=1.0，负向分支根本不参与计算，
# "No plump body" 这类词对结果毫无作用，只能靠正向词堆强度。
BODY_EMPHASIS = {
    "ultra": "Her body is extremely skinny and very thin, with almost no body fat at all.",
    "xxthin": "Her body is very thin and slim, with a narrow waist and slender legs.",
    "slim": "Her body is slim, slender and light.",
    "model": "Her body is tall, willowy and slim.",
    "normal": "Her body is lean and slim.",
    "curvy": "Her body is curvy, with a slim waist and a flat stomach.",
    "plus": "Her body is full and softly rounded.",
}

# 面孔族裔：不指定时模型会随机漂到欧美脸，国内审美下要固定成东亚。
FACE_PRESETS = {
    "cn": ("She is a Chinese woman with East Asian facial features, dark brown eyes, "
           "straight black hair, fair warm skin tone, natural Chinese facial structure, "
           "delicate Chinese features"),
    "kr": ("She is a Korean woman with Korean facial features, dark brown eyes, "
           "long straight black or dark brown hair, fair dewy skin, "
           "delicate Korean facial structure, k-beauty look"),
    "jp": ("She is a Japanese woman with Japanese facial features, dark brown eyes, "
           "dark straight hair, fair skin, natural Japanese facial structure"),
    "us": ("She is an American woman with Caucasian facial features, light or hazel eyes, "
           "natural American look, western facial structure"),
    "mix": ("She is a Eurasian mixed-race woman with blended East Asian and Caucasian "
            "features, brown eyes, soft mixed facial structure"),
}
FACE_ALIAS = {
    "cn": "cn", "中国": "cn", "china": "cn", "chinese": "cn",
    "kr": "kr", "韩国": "kr", "korea": "kr", "korean": "kr",
    "jp": "jp", "日本": "jp", "japan": "jp", "japanese": "jp",
    "us": "us", "美国": "us", "america": "us", "american": "us", "欧美": "us",
    "mix": "mix", "混血": "mix", "mixed": "mix",
}
DEFAULT_FACE = "cn"
# 腿部穿着（丝袜/打底裤/袜子）是最容易被吃掉的一类：半透明覆盖在皮肤上，
# 模型容易直接简化成裸腿。从服装关键词里抠出来单独成句才压得住。
LEGWEAR_WORDS = {"tights", "stocking", "stockings", "socks", "sock", "leggings",
                 "pantyhose", "knee-highs", "thigh-highs"}
COLOUR_WORDS = ("black", "white", "beige", "ivory", "brown", "grey", "gray", "navy",
                "nude", "cream", "red", "pink", "blue", "green", "burgundy")


def legwear_phrase(kw):
    """从服装关键词里提取腿部穿着，生成一句强约束。没有则返回空串。"""
    if not kw:
        return ""
    ws = [w for w in re.split(r"[\s,]+", str(kw).lower()) if w]
    i = next((n for n, w in enumerate(ws) if w in LEGWEAR_WORDS), -1)
    if i < 0:
        return ""
    # 颜色必须贴着 legwear 词往前找，否则会把上衣的颜色（white blouse）当袜子颜色
    col = ""
    for j in range(i - 1, max(-1, i - 6), -1):
        if ws[j] in COLOUR_WORDS:
            col = ws[j]
            break
    label = ("%s %s" % (col, ws[i])).strip()
    # 肤色系的连裤袜最危险：颜色贴着皮肤，模型干脆画成裸腿。必须要求"看得出是件衣服"。
    skin_like = ""
    if col in ("beige", "nude", "cream", "ivory", "tan", "skin"):
        skin_like = (" Even though the colour is close to her skin, it must still read clearly as a "
                     "garment: matte finish, a visible waistband or cuff, a subtle edge and sheen "
                     "that plain bare skin does not have.")
    # 不能写 dead "fully covered"，那会把大腿袜/及膝袜一律拉成连裤袜。
    # 长度只能交给参考图：它到哪儿就停在哪儿。
    return ("Her legs are NOT bare: reproduce the legwear from the outfit reference exactly - "
            "same item, same %s, same sheen, same transparency, and above all the SAME LENGTH. "
            "It must end at exactly the same height as in the reference: ankle, calf, knee, "
            "mid-thigh or waist. Do not lengthen it and do not shorten it - never turn short "
            "socks into full tights, and never cut full tights into thigh-highs.%s"
            % (label, skin_like))
NEG_TAIL = ("No mannequin, no doll, no plastic or grey body, no ball joints, no headless figure, "
            "no visible outlines, no sketch lines, no drawing, no text, no watermark.")


def build_prompt(user, roles, body=DEFAULT_BODY, pose_mode="both", face=DEFAULT_FACE, costume_kw=""):
    key = BODY_ALIAS.get(str(body).strip().lower(), DEFAULT_BODY)
    fkey = FACE_ALIAS.get(str(face).strip().lower(), DEFAULT_FACE)
    parts = []
    if user.strip():
        parts.append(user.strip())
    # 给了真人参考图就以参考图的脸为准，别让族裔描述跟它打架
    if not any(r == "identity" for r, _ in roles):
        parts.append(FACE_PRESETS.get(fkey, FACE_PRESETS[DEFAULT_FACE]))
    # 体型紧跟用户描述，避免被后面的服装/姿势描述稀释
    parts.append(BODY_PRESETS.get(key, BODY_PRESETS[DEFAULT_BODY]))
    has_skel = any(r == "skeleton" for r, _ in roles)
    for role, idx in roles:
        if role == "skeleton":
            # 骨架必须说清"只是定位图、别画进画面"，否则模型会把彩点棍子画到人身上
            parts.append(
                "Picture %d is a pose guide made of dots and sticks. It is ONLY a positioning map "
                "for the joints and limbs - it is NOT part of the picture: never draw the dots, "
                "the sticks, the colored joints or any skeleton line on her body. Use it solely to "
                "decide where each limb goes; the hands, arms, legs and feet must sit exactly on "
                "those joints." % idx)
        elif role == "depth":
            parts.append(
                "Picture %d is a grayscale depth map of the same body giving its exact 3D volume "
                "and the depth order of the limbs." % idx)
        elif role == "line":
            parts.append(
                "Picture %d is the same pose drawn as line-art, giving the limb outlines and the "
                "joint angles." % idx)
        elif role == "pose":
            if pose_mode == "depth":
                parts.append(
                    "Picture %d is a grayscale depth map of a human body: it defines the exact 3D "
                    "pose, the limb placement, the hand and foot positions and the camera framing. "
                    "Ignore its grey shading and its flat rendering completely - do not paint the "
                    "result in grey. Recreate that exact pose with a photorealistic woman, "
                    "keeping the limbs anatomically correct." % idx)
            elif pose_mode == "both":
                # 线稿 + 深度双参考：深度给体积和遮挡关系，线稿给肢体边界和关节角度。
                # 实测单独用任一都做不出抱膝、单腿后抬这类大动作，两者同时给才稳。
                parts.append(
                    "Picture %d is a grayscale depth map of a human body giving the exact 3D volume, "
                    "the depth order of the limbs, the hand and foot positions and the camera "
                    "framing. Picture %d is the same pose drawn as line-art. Use BOTH pictures "
                    "together to recreate that exact pose with a photorealistic woman - copy the "
                    "limb positions and joint angles precisely, including a raised or bent leg. "
                    "Ignore their grey shading and line style completely: do not paint the result "
                    "grey and do not draw outlines." % (idx, idx + 1))
            else:
                parts.append(
                    "Picture %d is a grey line-art mannequin drawing that ONLY defines the body pose, "
                    "limb positions and camera framing. Do not copy its drawing style, do not copy "
                    "the mannequin's body shape, weight or proportions - only the pose. "
                    "Recreate that exact pose with a complete head, neck and body, "
                    "keeping the limbs anatomically correct." % idx)
        elif role == "pose_line":
            parts.append(
                "Picture %d is the same pose again, drawn as a line-art mannequin: it gives the "
                "exact limb outlines and joint angles. Combine it with the depth map - both "
                "describe one and the same pose." % idx)
        elif role == "costume":
            parts.append(
                "Picture %d shows the exact outfit to wear: reproduce every garment, colour, "
                "pattern and material exactly as shown, including tops, outerwear, skirts or "
                "trousers, socks or tights, bags, jewellery and the exact shoe style. "
                "Any bag, basket or pouch in the picture is a handbag carried in the hand - "
                "never worn on the head. If the reference has no hat, do not invent a hat. "
                "The figure in Picture %d is a headless mannequin - do not copy the missing head "
                "or the mannequin body shape, she has a complete head, neck and face." % (idx, idx))
        elif role == "identity":
            parts.append(
                "Picture %d is the real subject: use her face, facial features, hair colour and "
                "hair style exactly as shown." % idx)
    # 自定义上传的服装没有关键词可解析，给一句条件式兜底（不会误伤本来就露腿的搭配）
    lw = legwear_phrase(costume_kw)
    if not lw and any(r == "costume" for r, _ in roles):
        lw = ("If the outfit reference shows tights, stockings, socks or leggings on the legs, "
              "reproduce them exactly - same colour, same transparency, and the same length: they "
              "must end at exactly the same height as in the reference. An actual garment on her "
              "body - never omit them, never replace them with bare skin, never lengthen or "
              "shorten them.")
    if lw:
        parts.append(lw)
    if has_skel:
        parts.append("Rebuild that exact pose as a normal photograph of a real woman with clean "
                     "bare skin: no dots, no markers, no skeleton overlay, no guide lines anywhere.")
    parts.append(BASE_TAIL)
    parts.append(BODY_EMPHASIS.get(key, BODY_EMPHASIS[DEFAULT_BODY]))
    parts.append(BODY_NEG.get(key, BODY_NEG[DEFAULT_BODY]))
    parts.append(NO_EXTRA_LIMBS)
    parts.append(NEG_TAIL)
    return " ".join(parts)


# --------------------------------------------------------------------- 任务
JOBS = {}
JOB_LOCK = threading.Lock()
QUEUE = threading.Semaphore(int(CFG.get("concurrency", 1)))


def new_job():
    jid = time.strftime("%y%m%d-%H%M%S-") + uuid.uuid4().hex[:4]
    job = {"id": jid, "state": "queued", "stage": "排队中", "t0": time.time(),
           "images": [], "error": None, "log": []}
    with JOB_LOCK:
        JOBS[jid] = job
    return job


def log(job, msg):
    job["log"].append("%.1fs %s" % (time.time() - job["t0"], msg))
    job["log"] = job["log"][-40:]


def run_job(job, opt):
    try:
        with QUEUE:
            job["state"] = "running"
            job["stage"] = "准备参考图"
            log(job, "start")

            tmpd = os.path.join(TMP, job["id"])
            os.makedirs(tmpd, exist_ok=True)

            refs, roles = [], []
            costume_kw = ""
            custom = opt.get("custom") or {}
            pose_mode = opt.get("poseMode", "skeleton")
            job["poseMode"] = pose_mode
            body_key = BODY_ALIAS.get(str(opt.get("body", DEFAULT_BODY)).strip().lower(),
                                      DEFAULT_BODY)
            warp = BODY_WARP.get(body_key, 1.0)
            # 前端滑块可直接覆盖档位默认值（0.55~1.20）
            try:
                sp = float(opt.get("slimPct") or 0)
                if 0.55 <= sp <= 1.20:
                    warp = sp
            except (TypeError, ValueError):
                pass
            job["used_warp"] = round(warp, 2)

            has_id = bool(opt.get("identity"))

            def add_pose(src, key):
                """按模式塞入姿势参考图。参考图总数控制在 4 张以内（含服装/人脸）。"""
                if abs(warp - 1.0) > 0.01:
                    try:
                        src = slim_warp(src, warp, tmpd, key)
                        key = "%s_w%d" % (key, int(round(warp * 100)))
                        log(job, "slim warp %d%%" % int(round(warp * 100)))
                    except Exception:                              # noqa: BLE001
                        log(job, "slim warp failed")
                if pose_mode == "skeleton":
                    try:
                        sk = ensure_skeleton(src, key)
                        if sk:
                            refs.append(C.stage(sk, "atlas_sk_%s.png" % key))
                            roles.append(("skeleton", len(refs)))
                        # 骨架已含关节坐标，深度图是冗余的：实测去掉后质量不变，
                        # 单张从 87s 降到 56s（少一张参考图）。
                        refs.append(C.stage(src, "atlas_ln_%s%s" % (key, os.path.splitext(src)[1])))
                        roles.append(("line", len(refs)))
                        log(job, "skeleton+line")
                        return
                    except Exception:                              # noqa: BLE001
                        log(job, "skeleton failed, fallback")
                if pose_mode in ("depth", "both", "skeleton"):
                    try:
                        hit = os.path.isfile(os.path.join(DEPTH_CACHE, key + ".jpg"))
                        dep = ensure_depth(src, key)
                        dp = C.stage(dep, "atlas_pd_%s%s" % (key, os.path.splitext(dep)[1]))
                        lp = (C.stage(src, "atlas_pl_%s%s" % (key, os.path.splitext(src)[1]))
                              if pose_mode == "both" else None)
                        refs.append(dp)
                        roles.append(("pose", len(refs)))
                        if lp:
                            refs.append(lp)
                        log(job, "depth " + ("cached" if hit else "generated"))
                        return
                    except Exception:                              # noqa: BLE001
                        log(job, "depth failed, fallback to line art")
                refs.append(C.stage(src, "atlas_pose_%s%s" % (key, os.path.splitext(src)[1])))
                roles.append(("pose", len(refs)))

            if opt.get("pose"):
                p, v = find_pose(opt["pose"]["id"], opt["pose"].get("angle", "front"))
                src = os.path.join(POSE_DIR, v["full"].replace("/", os.sep))
                key = "%s_%s" % (p["id"], v["angle"])
                add_pose(src, key)
                job["used_pose"] = "%s %s" % (p["id"], v["angle"])
            elif custom.get("pose"):
                src = custom["pose"]
                key = "u_%s" % os.path.splitext(os.path.basename(src))[0]
                add_pose(src, key)
                job["used_pose"] = "自定义"
            if opt.get("costume"):
                c = find_costume(opt["costume"]["id"])
                src = os.path.join(COSTUME_DIR, c["画像"].replace("/", os.sep))
                refs.append(C.stage(src, "atlas_costume_%s%s" % (c["id"], os.path.splitext(src)[1])))
                roles.append(("costume", len(refs)))
                job["used_costume"] = c["id"]
                costume_kw = c.get("キーワード", "")
            elif custom.get("costume"):
                src = custom["costume"]
                refs.append(C.stage(src, "atlas_custom_costume" + os.path.splitext(src)[1]))
                roles.append(("costume", len(refs)))
                job["used_costume"] = "自定义"
            identity_path = None
            identity_ref = None
            if opt.get("identity"):
                identity_path = opt["identity"]["path"]
                # 整张全身照直接当参考等于没给：864x1536 里脸只有几十像素，
                # flux 取不到身份信息。先裁成正脸特写再当 Picture N。
                face_crop = os.path.join(OUT, job["id"], "identity_face.png")
                try:
                    os.makedirs(os.path.dirname(face_crop), exist_ok=True)
                    FR.crop_face(identity_path, face_crop, 0.18, 0.06, 0.14, 2, None)
                    identity_ref = face_crop
                    log(job, "identity cropped to face")
                except SystemExit:
                    identity_ref = identity_path
                    log(job, "identity: no face found, use raw photo")
                except Exception:                                  # noqa: BLE001
                    identity_ref = identity_path
                    log(job, "identity crop failed, use raw photo")
                refs.append(C.stage(identity_ref,
                                    "atlas_identity" + os.path.splitext(identity_ref)[1]))
                roles.append(("identity", len(refs)))

            w, h = C.clamp_size(int(opt.get("width", 832)), int(opt.get("height", 1216)))
            prompt = build_prompt(opt.get("prompt", ""), roles, opt.get("body", DEFAULT_BODY),
                                  pose_mode, opt.get("face", DEFAULT_FACE), costume_kw)
            job["used_body"] = opt.get("body", DEFAULT_BODY)
            job["used_face"] = opt.get("face", DEFAULT_FACE)
            seed = int(opt.get("seed") or 1)
            steps = int(opt.get("steps", 10))

            job["stage"] = "生成中"
            log(job, "graph refs=%d %s" % (len(refs), "x".join(map(str, (w, h)))))
            graph = build_gen(prompt, refs, w, h, seed, steps, job["id"])
            imgs = None
            for attempt in range(2):
                try:
                    imgs = C.submit(graph, verbose=False)
                    break
                except Exception as e:                             # noqa: BLE001
                    if attempt == 0:
                        log(job, "submit retry: %s" % str(e).splitlines()[0][:80])
                        time.sleep(2)
                    else:
                        raise
            base = C.pull(imgs, tmpd, stem="base")[0]
            log(job, "pass1 %.1fs" % (time.time() - job["t0"]))

            final = base
            swap_note = "已换脸"
            if opt.get("doSwap") and identity_path:
                job["stage"] = "换脸精修"
                try:
                    crop = os.path.join(tmpd, "face_ref.png")
                    try:
                        FR.crop_face(identity_path, crop, 0.18, 0.06, 0.14, 2, None)
                    except SystemExit:
                        crop = identity_path
                    info = smart_head_mask(final, job["id"])
                    if info.get("error"):
                        swap_note = "未找到头部，跳过换脸"
                        log(job, "no face for swap -> skip")
                    else:
                        log(job, "mask via %s" % info.get("mode", "?"))
                        W, H = info["size"]
                        g = FS.build(info["target"], info["mask"], C.stage(crop),
                                    W - W % 8, H - H % 8, FS.DEFAULT_PROMPT,
                                    seed + 7, "%s_swap" % job["id"], 4,
                                    float(opt.get("swapStrength", 0.95)), 1.0, None, 0.35)
                        out2 = C.pull(C.submit(g, verbose=False), tmpd, stem="final")
                        if out2:
                            final = out2[0]
                            log(job, "pass2 ok")
                        else:
                            swap_note = "换脸没有产出图片"
                except Exception:                                  # noqa: BLE001
                    swap_note = "换脸报错，已保留底图"
                    log(job, "swap failed: %s" % traceback.format_exc(limit=2).splitlines()[-1])
            elif not identity_path:
                swap_note = "没有人脸照，未换脸"
            else:
                swap_note = "未勾选换脸精修"

            # 只把成品写进 work/outputs/<日期>/，中间文件留在 work/tmp/<job>
            d = out_dir()
            stem = out_stem()

            def _url(p):
                return "/" + os.path.relpath(p, HERE).replace(os.sep, "/")

            out_final = os.path.join(d, stem + "_成品.jpg")
            shutil.copy2(final, out_final)
            job["images"] = [{"url": _url(out_final), "kind": "final"}]
            swapped = (final != base)
            if swapped:
                out_base = os.path.join(d, stem + "_底图.jpg")
                shutil.copy2(base, out_base)
                job["images"].append({"url": _url(out_base), "kind": "base"})
            job["swapped"] = swapped
            job["swapNote"] = swap_note
            job["state"] = "done"
            job["stage"] = "完成"
            job["elapsed"] = round(time.time() - job["t0"], 1)
            log(job, "done %.1fs" % job["elapsed"])
    except Exception as e:                                         # noqa: BLE001
        job["state"] = "error"
        job["stage"] = "失败"
        job["error"] = str(e)[:800]
        log(job, "ERROR %s" % e)
        traceback.print_exc()


def smart_head_mask(img_path, tag, blur=22):
    """定位头部并生成遮罩。

    技能自带的 headmask.make_one 用 Haar 在整图上找最大人脸，人像全身图里
    经常误检到腿部/裙摆（实测 bbox 落在 y=805，遮罩完全没盖到头，换脸等于没跑）。
    这里改成：先用背景差分取出人形，再只在顶部 55% 找脸并做合理性校验，
    找不到就按人形轮廓推算头位。棚拍纯色背景场景下两种路径都稳。
    """
    import cv2
    import numpy as np
    from PIL import Image, ImageDraw, ImageFilter

    im_pil = Image.open(img_path).convert("RGB")
    W, H = im_pil.size
    bgr = cv2.imread(img_path, cv2.IMREAD_COLOR)

    # --- 1. 背景差分取人形，拿到身体框与腰线中心
    k = max(8, min(24, W // 20))
    corners = np.concatenate([bgr[:k, :k].reshape(-1, 3), bgr[:k, -k:].reshape(-1, 3),
                              bgr[-k:, :k].reshape(-1, 3), bgr[-k:, -k:].reshape(-1, 3)])
    bg = np.median(corners, axis=0)
    diff = np.abs(bgr.astype(np.int16) - bg.astype(np.int16)).max(axis=2)
    m = (diff > 28).astype(np.uint8)
    m = cv2.morphologyEx(m, cv2.MORPH_OPEN, np.ones((5, 5), np.uint8))
    m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, np.ones((15, 15), np.uint8))
    body = None
    n, lab, stats, _ = cv2.connectedComponentsWithStats(m, 8)
    if n > 1:
        i = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
        x0, y0, bw, bh, area = stats[i]
        if area > 0.02 * W * H:
            sub = (lab == i).astype(np.uint8)
            band = sub[y0 + int(bh * 0.45): y0 + int(bh * 0.55)]
            xs = np.nonzero(band)[1]
            body = {"x0": x0, "y0": y0, "w": bw, "h": bh,
                    "cx": int(np.median(xs)) if len(xs) else x0 + bw // 2}

    # --- 2. 只在顶部找脸，并校验它没跑到身体中段
    face = None
    gray = cv2.cvtColor(bgr[:int(H * 0.55)], cv2.COLOR_BGR2GRAY)
    cascade = cv2.CascadeClassifier(
        cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    faces = cascade.detectMultiScale(gray, scaleFactor=1.06, minNeighbors=5, minSize=(30, 30))
    faces = [f for f in faces if 0.05 * W < f[2] < 0.42 * W]
    if faces:
        faces.sort(key=lambda f: f[1])
        x, y, w, h = faces[0]
        if body is None or y < body["y0"] + 0.25 * body["h"]:
            face = (int(x), int(y), int(w), int(h))

    # --- 3. 生成椭圆遮罩
    mask = Image.new("L", (W, H), 0)
    d = ImageDraw.Draw(mask)
    if face:
        x, y, w, h = face
        cx, cy = x + w / 2.0, y + h / 2.0
        rx, ry = w * 0.95, h * 1.25
        mode = "haar"
    elif body:
        cx = body["cx"]
        cy = body["y0"] + 0.075 * body["h"]
        rx, ry = 0.062 * body["h"], 0.10 * body["h"]
        mode = "silhouette"
    else:                                     # 兜底：交给技能里的老实现
        info = HM.make_one(img_path, 0.80, 1.80, 1.30, blur, tag=tag)
        return info
    d.ellipse([cx - rx, cy - ry, cx + rx, cy + ry], fill=255)
    mask = mask.filter(ImageFilter.GaussianBlur(blur))

    target = C.stage(img_path, "tgt_%s.png" % tag)
    mname = "hsm2_%s.png" % tag
    mask.convert("RGB").save(os.path.join(C.IN_DIR, mname))
    return {"target": target, "mask": mname, "size": [W, H], "mode": mode,
            "box": [int(cx - rx), int(cy - ry), int(cx + rx), int(cy + ry)]}


def slim_warp(src, ratio, outdir, tag):
    """把参考图里的人横向压窄，等于直接让模型"看到"一个更瘦的人。

    画布尺寸必须保持不变、两侧补背景色：一旦图变小再喂进去，
    模型会 resize 回目标宽高，压缩就白做了。
    """
    import numpy as np
    from PIL import Image
    if abs(ratio - 1.0) < 0.01:
        return src
    im = Image.open(src).convert("RGB")
    W, H = im.size
    nw = max(8, int(round(W * ratio)))
    small = im.resize((nw, H), Image.LANCZOS)
    a = np.asarray(im); k = 8
    corners = np.concatenate([a[:k, :k].reshape(-1, 3), a[:k, -k:].reshape(-1, 3),
                              a[-k:, :k].reshape(-1, 3), a[-k:, -k:].reshape(-1, 3)])
    bg = tuple(int(v) for v in np.median(corners, axis=0))
    canvas = Image.new("RGB", (W, H), bg)
    canvas.paste(small, ((W - nw) // 2, 0))
    dst = os.path.join(outdir, "pose_%s_w%d.png" % (tag, int(round(ratio * 100))))
    canvas.save(dst)
    return dst


def ensure_skeleton(src_path, key):
    """线稿人台 → SDPose 全身骨架图（带缓存）。

    SDPose 的 WholeBody 模型能直接在人台灰度图上检出全部关节点（含双手手指、
    双脚脚趾），输出就是 OpenPose 那种"点 + 棍"的骨架。骨架给的是关节坐标而
    不是表面纹理，模型照着摆肢体的准确度比线稿/深度都高：实测"抱膝至胸"、
    "单腿后抬"这类动作，三参考做不出来，加上骨架后能准确还原。
    """
    dst = os.path.join(SKEL_CACHE, key + "_skel.png")
    # C.pull 落到磁盘的实际是 jpg，两种后缀都要认，否则缓存永远不命中
    for ext in (".png", ".jpg"):
        p = os.path.join(SKEL_CACHE, key + "_skel" + ext)
        if os.path.isfile(p):
            return p
    staged = C.stage(src_path, "skin_%s%s" % (key, os.path.splitext(src_path)[1]))
    g = {
        "1": {"class_type": "LoadImage", "inputs": {"image": staged}},
        "2": {"class_type": "SDPoseOODLoader",
              "inputs": {"model_type": "WholeBody", "unet_precision": "fp16",
                         "device": "auto", "unload_on_finish": True}},
        "3": {"class_type": "SDPoseOODProcessor",
              "inputs": {"sdpose_model": ["2", 0], "images": ["1", 0],
                         "score_threshold": 0.3, "overlay_alpha": 1.0, "batch_size": 1,
                         "prompt": "person .", "gd_threshold": 0.3,
                         "keep_face": True, "keep_hands": True, "keep_feet": True,
                         "scale_for_xinsr": False}},
        "10": {"class_type": "SaveImage",
               "inputs": {"images": ["3", 0], "filename_prefix": "sk_" + key}},
    }
    out = C.pull(C.submit(g, verbose=False), SKEL_CACHE, stem=key + "_skel")
    return out[0] if out else None


def ensure_depth(src_path, key):
    """线稿人台 → 深度图（带缓存）。

    DepthAnythingV2 对 3D 人台的体积还原很准：四肢、手、脚的纵深都清楚。
    直接拿线稿当参考时模型只看得到细线条，四肢位置容易飘；换成深度图后
    姿态约束明显更硬。同一姿势只算一次，之后直接读缓存。
    """
    dst = os.path.join(DEPTH_CACHE, key + ".jpg")
    if os.path.isfile(dst):
        return dst
    staged = C.stage(src_path, "depthin_%s%s" % (key, os.path.splitext(src_path)[1]))
    g = {
        "1": {"class_type": "LoadImage", "inputs": {"image": staged}},
        "2": {"class_type": "DepthAnythingV2Preprocessor",
              "inputs": {"image": ["1", 0],
                         "ckpt_name": "depth_anything_v2_vitl.pth",
                         "resolution": 1024}},
        "3": {"class_type": "SaveImage",
              "inputs": {"images": ["2", 0], "filename_prefix": "depth_" + key}},
    }
    out = C.pull(C.submit(g, verbose=False), DEPTH_CACHE, stem=key)
    return out[0] if out else src_path


def build_gen(prompt, refs, w, h, seed, steps, prefix):
    g = C.loaders()
    mode = "%d_image" % max(1, len(refs))
    inputs = {"clip": ["2", 0], "vae": ["3", 0], "prompt": prompt, "mode": mode,
              "batch_size": 1, "width": int(w), "height": int(h)}
    for i, r in enumerate(refs, 1):
        nid = str(10 + i)
        g[nid] = {"class_type": "LoadImage", "inputs": {"image": r}}
        inputs["image%d" % i] = [nid, 0]
    g["20"] = {"class_type": "PainterFluxImageEdit", "inputs": inputs}
    g["30"] = {"class_type": "KSampler",
               "inputs": C.sampler("1", ("20", 0), ("20", 1), ("20", 2), seed, steps)}
    g["40"] = {"class_type": "VAEDecode", "inputs": {"samples": ["30", 0], "vae": ["3", 0]}}
    g["50"] = {"class_type": "SaveImage", "inputs": {"images": ["40", 0], "filename_prefix": prefix}}
    return g


# -------------------------------------------------------------------- 路由
def json_items_pose(q, series, cat, angle, offset, limit):
    data = poses()
    pool = data["poses"]
    if series:
        pool = [p for p in pool if p["series"] == series]
    if cat:
        pool = [p for p in pool if p["category"] == cat]
    if q:
        terms = [t for t in q.replace("，", " ").split() if t]
        scored = []
        for p in pool:
            hay = (" ".join(str(p["names"].get(k, "")) for k in ("zh", "en", "ja"))
                   + " " + p["id"] + " " + p["no"] + " " + p["category"]).lower()
            hits = sum(1 for t in terms if t.lower() in hay)
            if hits:
                scored.append((hits, p))
        scored.sort(key=lambda x: -x[0])
        pool = [p for _, p in scored]
    a = ANGLE_ALIAS.get(str(angle).lower(), str(angle)) if angle else None
    if a:
        pool = [p for p in pool if any(v["angle"] == a for v in p["variants"])]
    total = len(pool)
    page = pool[offset:offset + limit]
    items = []
    for p in page:
        v = next((x for x in p["variants"] if x["angle"] == a), p["variants"][0]) if a else p["variants"][0]
        # 每个机位的真实缩略图/大图都要给前端：文件名是 _diag 而不是 _diagonal，
        # 前端靠字符串替换拼路径会 404，必须由后端给出真实路径。
        vm = {x["angle"]: {"thumb": "/a/pose/" + x["thumb"], "full": "/a/pose/" + x["full"]}
              for x in p["variants"]}
        items.append({"id": p["id"], "no": p["no"], "name": p["names"].get("zh", ""),
                      "nameEn": p["names"].get("en", ""), "category": p["category"],
                      "series": p["series"], "angle": v["angle"],
                      "angles": [x["angle"] for x in p["variants"]],
                      "variants": vm,
                      "thumb": "/a/pose/" + v["thumb"], "full": "/a/pose/" + v["full"]})
    return {"total": total, "items": items}


def json_items_costume(q, cat, offset, limit):
    data = costumes()
    pool = data
    if cat:
        pool = [c for c in pool if c["カテゴリ"] == cat]
    if q:
        terms = [t for t in q.lower().replace("，", " ").split() if t]
        pool = [c for c in pool
                if all(t in (c["キーワード"] + " " + c["id"] + " " + c["カテゴリ"]).lower() for t in terms)]
    total = len(pool)
    page = pool[offset:offset + limit]
    items = [{"id": c["id"], "category": c["カテゴリ"], "kw": c["キーワード"],
              "thumb": "/a/costume/" + c["サムネ"], "full": "/a/costume/" + c["画像"]} for c in page]
    return {"total": total, "items": items}


def safe_join(root, rel):
    rel = urllib.parse.unquote(rel).replace("\\", "/")
    if ".." in rel.split("/"):
        return None
    p = os.path.abspath(os.path.join(root, rel))
    return p if p.startswith(os.path.abspath(root)) else None


MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".webp": "image/webp", ".gif": "image/gif", ".bmp": "image/bmp",
        ".svg": "image/svg+xml", ".ico": "image/x-icon",
        ".html": "text/html; charset=utf-8", ".htm": "text/html; charset=utf-8",
        ".css": "text/css; charset=utf-8", ".js": "application/javascript; charset=utf-8",
        ".json": "application/json; charset=utf-8"}


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    # ---- helpers
    def _cors(self):
        """允许 file:// 或其它端口打开的页面直接取数据与图片。"""
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Filename")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Cross-Origin-Resource-Policy", "cross-origin")

    def send_json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def send_file(self, path, cache=0):
        if not path or not os.path.isfile(path):
            self.send_json({"error": "not found"}, 404)
            return
        ext = os.path.splitext(path)[1].lower()
        size = os.path.getsize(path)
        ctype = MIME.get(ext, "application/octet-stream")
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(size))
        if ctype.startswith("text/html") or abs(cache) == 0:
            self.send_header("Cache-Control", "no-store, must-revalidate")
        else:
            self.send_header("Cache-Control", "public, max-age=%d" % cache)
        self._cors()
        self.end_headers()
        with open(path, "rb") as f:
            while True:
                chunk = f.read(262144)
                if not chunk:
                    break
                self.wfile.write(chunk)

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    # ---- GET
    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(u.query)
        path = u.path

        def g(k, d=None):
            return (qs.get(k) or [d])[0]

        try:
            if path in ("/", "/index.html"):
                return self.send_file(os.path.join(HERE, "index.html"))
            if path in ("/api/ping", "/health"):
                return self.send_json({"ok": True, "app": "atlas-studio", "port": PORT})
            if path == "/api/meta":
                pd = poses()
                return self.send_json({
                    "poses": pd["totals"]["poses"], "costumeCount": len(costumes()),
                    "series": [{"key": s["key"], "name": s["names"].get("zh")} for s in pd["series"]],
                    "categories": sorted({p["category"] for p in pd["poses"]}),
                    "costumeCats": sorted({c["カテゴリ"] for c in costumes()}),
                    "engine": CFG.get("engine", "FLUX.2 KLEIN"),
                    "comfy": C.alive(), "api": C.API})
            if path == "/api/poses":
                return self.send_json(json_items_pose(
                    g("q", "").strip(), g("series"), g("category"), g("angle"),
                    int(g("offset", 0)), min(120, int(g("limit", 36)))))
            if path == "/api/costumes":
                return self.send_json(json_items_costume(
                    g("q", "").strip(), g("cat"), int(g("offset", 0)),
                    min(120, int(g("limit", 36)))))
            if path.startswith("/a/pose/"):
                return self.send_file(safe_join(POSE_DIR, path[len("/a/pose/"):]), 604800)
            if path.startswith("/a/costume/"):
                return self.send_file(safe_join(COSTUME_DIR, path[len("/a/costume/"):]), 604800)
            if path.startswith("/work/"):
                return self.send_file(safe_join(WORK, path[len("/work/"):]), 0)
            if path.startswith("/api/job/"):
                job = JOBS.get(path.rsplit("/", 1)[-1])
                if not job:
                    return self.send_json({"error": "no such job"}, 404)
                out = {k: job[k] for k in ("id", "state", "stage", "images", "error", "log",
                                           "swapped", "swapNote") if k in job}
                out["elapsed"] = round(time.time() - job["t0"], 1)
                return self.send_json(out)
            return self.send_json({"error": "bad route"}, 404)
        except Exception as e:                                     # noqa: BLE001
            traceback.print_exc()
            return self.send_json({"error": str(e)}, 500)

    # ---- POST
    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        n = int(self.headers.get("Content-Length") or 0)
        if n > 40 * 1024 * 1024:
            return self.send_json({"error": "too large"}, 413)
        raw = self.rfile.read(n) if n else b""
        try:
            if u.path == "/api/upload":
                name = urllib.parse.unquote(self.headers.get("X-Filename") or "upload.png")
                ext = os.path.splitext(name)[1].lower() or ".png"
                if ext not in MIME:
                    return self.send_json({"error": "只收图片"}, 400)
                fn = "u_%s%s" % (uuid.uuid4().hex[:10], ext)
                dst = os.path.join(UP, fn)
                with open(dst, "wb") as f:
                    f.write(raw)
                from PIL import Image
                im = Image.open(dst)
                size = list(im.size)
                real = (im.format or "").lower()
                im.close()                      # Windows 下不关句柄就改不了名
                want = {"jpeg": ".jpg", "png": ".png", "webp": ".webp"}.get(real)
                if want and want != ext:
                    dst2 = os.path.splitext(dst)[0] + want
                    os.replace(dst, dst2)
                    dst = dst2
                return self.send_json({"path": dst, "url": "/work/uploads/" + os.path.basename(dst),
                                       "size": size, "name": name})
            if u.path == "/api/generate":
                opt = json.loads(raw.decode("utf-8") or "{}")
                if not C.alive():
                    return self.send_json({"error": "ComfyUI 未启动（127.0.0.1:8188 无响应）"}, 503)
                job = new_job()
                threading.Thread(target=run_job, args=(job, opt), daemon=True).start()
                return self.send_json({"job": job["id"]})
            return self.send_json({"error": "bad route"}, 404)
        except Exception as e:                                     # noqa: BLE001
            traceback.print_exc()
            return self.send_json({"error": str(e)}, 500)


def clean_tmp(days=3):
    """清掉几天前的中间产物，避免 work/tmp 无限膨胀。"""
    limit = time.time() - days * 86400
    try:
        for name in os.listdir(TMP):
            p = os.path.join(TMP, name)
            try:
                if os.path.getmtime(p) < limit:
                    shutil.rmtree(p, ignore_errors=True)
            except OSError:
                pass
    except OSError:
        pass


def main():
    clean_tmp()
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), H)
    print("Atlas Studio -> http://127.0.0.1:%d/" % PORT)
    print("  姿态库 %s | 衣装库 %s" % (POSE_DIR, COSTUME_DIR))
    print("  ComfyUI %s %s" % (C.API, "OK" if C.alive() else "未启动"))
    srv.serve_forever()


if __name__ == "__main__":
    main()
