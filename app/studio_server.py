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
import random
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


def _abs(p):
    """相对路径按 config.json 所在目录解析 —— 整个文件夹搬到别处也能直接跑。"""
    p = str(p)
    return p if os.path.isabs(p) else os.path.normpath(os.path.join(HERE, p))


PORT = int(CFG.get("port", 8125))
SKILL = _abs(CFG["skill_dir"])
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

# --------------------------------------------------------------- 预设 / 元数据
PRESET_PATH = os.path.join(HERE, "presets.json")


def load_presets():
    try:
        with open(PRESET_PATH, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def save_presets(d):
    tmp = PRESET_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
    os.replace(tmp, PRESET_PATH)


def write_meta(img_path, meta):
    """在成品旁边写一份同名 .json，记下全部参数，方便日后追溯和重跑。"""
    p = os.path.splitext(img_path)[0] + ".json"
    try:
        with open(p, "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)
        return p
    except OSError:
        return None


def history(limit=80):
    """按时间倒序读出历史出图（靠旁边的 .json 还原参数）。"""
    items = []
    try:
        days = sorted((d for d in os.listdir(OUTROOT)
                       if re.match(r"^\d{4}-\d{2}-\d{2}$", d)), reverse=True)
    except OSError:
        return items
    for day in days[:30]:
        dp = os.path.join(OUTROOT, day)
        try:
            names = [n for n in os.listdir(dp) if n.endswith(".json")]
        except OSError:
            continue
        for fn in names:
            p = os.path.join(dp, fn)
            img = os.path.splitext(p)[0] + ".jpg"
            if not os.path.isfile(img):
                continue
            try:
                with open(p, encoding="utf-8") as f:
                    m = json.load(f)
            except (OSError, ValueError):
                continue
            m["url"] = "/" + os.path.relpath(img, HERE).replace(os.sep, "/")
            m["day"] = day
            items.append(m)
        if len(items) >= limit * 3:
            break
    items.sort(key=lambda m: (m.get("time") or ""), reverse=True)
    return items[:limit]

POSE_DIR = _abs(CFG["pose_dir"])
COSTUME_DIR = _abs(CFG["costume_dir"])

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
             "each leg ends in a single foot wearing a single shoe, "
             "exactly two arms and two hands, one arm on each side of her body, "
             "nothing else growing out of her sleeves or her coat, "
             "when her arms are raised her sleeves hang empty and no hand appears at the end "
             "of a hanging sleeve, "
             "no third arm and no extra hand anywhere in the picture.")

# 腿部结构容易崩（交叉腿/并拢腿常画出第三条腿），单独一条负向压住
NO_EXTRA_LIMBS = ("No third leg, no extra leg, no duplicated limb, no extra foot, no three feet, "
                  "no fused or merged legs, no ghost leg, no extra arm, no third arm, "
                  "no extra hand, no disembodied hand gripping a bag, "
                  "no mutated or conjoined anatomy.")

# 体型预设：不写身材时模型会给"平均体型"，在棚拍全身图里显壮。
# 每个档位都配一句负向，压住反方向的体型。
BODY_PRESETS = {
    "ultra": ("extremely skinny body with almost no body fat, very thin waist and narrow hips, "
              "completely flat stomach, pronounced thigh gap, very thin slender arms and legs, "
              "visible collarbones and shoulder blades, small delicate frame, "
              "runway editorial skinny silhouette, no curves at all, "
              "paper-thin body, not a trace of softness, every part of her body is slender and flat"),
    "xxthin": ("very thin slim figure, narrow waist, completely flat stomach, clear thigh gap, "
               "thin slender legs and arms, visible collarbones, delicate shoulders, "
               "small light frame, very slim idol body proportions, no body fat, "
               "paper-thin slender body, no soft flesh anywhere, lean and delicate frame"),
    "slim": ("slim slender figure, narrow waist, flat stomach, slim thighs with thigh gap, "
             "long slender legs, delicate shoulders, light frame, "
             "slim k-pop idol proportions, no extra weight anywhere, "
             "very slim and slender body, tight and lean, not a single soft or thick part"),
    "model": ("tall high-fashion model proportions, very long slim legs, slim toned body, "
              "small waist, narrow elegant silhouette, willowy slender frame, "
              "razor slim body, lean flat torso, no fullness anywhere"),
    "normal": ("lean healthy figure on the slim side, balanced proportions, fit and slim, "
               "neither thin nor heavy, no fullness, "
               "slim and well-proportioned body, lean torso, trimmed waistline, no soft mass"),
    "curvy": ("curvy figure with a clearly narrow waist, fuller bust and hips, flat stomach, "
              "soft curves without any heaviness, slim-waisted hourglass, "
              "slim upper body with a tiny waist, toned and lean everywhere else, "
              "curves from the bust and hips only, no extra fat"),
    "plus": ("fuller plus-size figure, soft rounded body, wide hips, full thighs, "
             "generous curves, firm and well proportioned, "
             "full but firm and toned, no sagging, still a slim waistline"),
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
    "ultra": 0.68,
    "xxthin": 0.75,
    "slim": 0.80,
    "model": 0.79,
    "normal": 0.85,
    "curvy": 0.90,
    "plus": 0.94,
}
# 体型在提示词里出现两次：紧跟用户描述（权重最高的一段）＋结尾再强调一次。
# 之所以必须重复，是因为本模型 cfg=1.0，负向分支根本不参与计算，
# "No plump body" 这类词对结果毫无作用，只能靠正向词堆强度。
BODY_EMPHASIS = {
    "ultra": "Her body is extremely skinny and very thin, with almost no body fat at all. "
             "Every part of her body is paper-thin and delicate.",
    "xxthin": "Her body is very thin and slim, with a narrow waist and slender legs. "
              "She has no soft flesh anywhere - she is paper-thin.",
    "slim": "Her body is slim, slender and light, tight and lean with no soft parts.",
    "model": "Her body is tall, willowy and slim, razor thin with a flat lean torso.",
    "normal": "Her body is lean and slim, well-proportioned and trimmed.",
    "curvy": "Her body is curvy with a slim waist and a flat stomach, but still lean and toned "
             "everywhere else - the curves come from her bust and hips only.",
    "plus": "Her body is full but firm and toned, with a slim waistline.",
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


# ---- 身高 / 腿身比 / 三围：在体型档之上做更细的雕琢
HEIGHT_PRESETS = {
    "petite": "petite short stature around 155 cm, small compact frame, short torso",
    "normal": "average height around 163 cm, balanced frame",
    "tall": "tall stature around 170 cm, elongated frame, long limbs",
    "super": "very tall stature around 178 cm, fashion model height, very long limbs",
}
# 第二项 k：参考图腰线以下纵向拉伸倍数，直接改腿长（prompt 说得再好也不如直接拉）
LEG_PRESETS = {
    "short": ("short legs relative to the torso, compact lower body, low waistline", 0.90),
    "normal": ("balanced leg to body ratio, natural waistline", 1.00),
    "long": ("long legs, high waistline, legs longer than the torso", 1.12),
    "verylong": ("extremely long legs, runway model proportions, legs clearly longer than the torso", 1.24),
}
DEFAULT_HEIGHT = "normal"
DEFAULT_LEG = "normal"


def bust_desc(v):
    """v 越大胸越大。分档比原来更细，两端更极端。"""
    if v < 15:
        return "completely flat chest, no bust at all, flat chest line"
    if v < 35:
        return "very small flat bust, barely-there chest"
    if v < 52:
        return "small bust, slim chest"
    if v < 68:
        return "medium bust, natural chest"
    if v < 84:
        return "large bust, full and prominent breasts"
    return "very large bust, big prominent breasts, generous cleavage"


def waist_desc(v):
    """v 越大腰越细。"""
    if v < 20:
        return "thicker waist"
    if v < 40:
        return "average waist"
    if v < 65:
        return "narrow waist"
    if v < 85:
        return "very narrow waist, clearly cinched"
    return "extremely narrow waist, tiny waistline"


def hip_desc(v):
    """v 越大臀越宽。分档比原来更细，两端更极端。"""
    if v < 15:
        return "extremely narrow hips, flat straight hip line, no hip curve"
    if v < 35:
        return "narrow hips, slim hip line"
    if v < 52:
        return "slim hips, light hip curve"
    if v < 68:
        return "average hips, natural hip curve"
    if v < 84:
        return "wide full hips, pronounced hip curve, full buttocks"
    return "very wide prominent hips, large rounded buttocks, strong hourglass"


def legwear_phrase(kw, half=False):
    """从服装关键词里提取腿部穿着，生成一句强约束。没有则返回空串。"""
    if not kw or half:                 # 半身取景不画腿脚，整段跳过
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
    # 这套衣装库里的 "socks" 一律是及膝袜（实测 SPR-0001/0004/0005/0006/0010 全是膝下）。
    # 关键词里从不写高度，所以按词本身给一个**唯一**的高度，绝不给"菜单"——
    # 列 ankle/calf/knee/mid-thigh 会让模型想同时满足，一条腿上长出三道袜口。
    word = ws[i]
    if word in ("tights", "pantyhose"):
        height = ("full tights: one waistband at her waist under the skirt and the fabric running "
                  "unbroken down into her shoes")
    elif word == "leggings":
        height = ("leggings covering her from the waist down to her ankles, with a single hem at "
                  "the ankle")
    elif word in ("stocking", "stockings"):
        height = ("stockings whose single cuff sits at mid-thigh, with bare skin above it")
    else:                                   # socks / sock
        height = ("knee-high socks whose single cuff sits just below her knee, with bare thigh "
                  "skin above them")
    # 肤色系的连裤袜最危险：颜色贴着皮肤，模型干脆画成裸腿。必须要求"看得出是件衣服"。
    skin_like = ""
    if col in ("beige", "nude", "cream", "ivory", "tan", "skin"):
        skin_like = (" Even though the colour is close to her skin, it must still read clearly as a "
                     "garment: matte finish, a visible waistband or cuff, a subtle edge and sheen "
                     "that plain bare skin does not have.")
    # 只说"她穿着什么"这一句正向描述。不再写"绝不允许出现第二个袜口 / 不许叠穿"这类列举：
    # 实测每加一句禁止，模型反而更容易把那个东西画出来（越描越黑）。
    return ("Her legs are NOT bare: she wears %s - %s. Match the outfit reference exactly.%s"
            % (label, height, skin_like))
# 鞋 / 包 / 首饰是第二类容易丢的东西：它们在画面里占比小、位置分散，
# 提示词里那句 "bags, jewellery and the exact shoe style" 早被前面的描述稀释掉了。
# 同样从关键词里抠出来单独成句，还带上颜色。
SHOE_WORDS = {"sneakers", "sneaker", "pumps", "boots", "boot", "loafers", "loafer",
              "sandals", "sandal", "heels", "heel", "mules", "flats", "slippers",
              "moccasins", "oxfords", "espadrilles", "brogues"}
BAG_WORDS = {"bag", "tote", "pouch", "clutch", "basket", "backpack", "handbag",
             "shoulder", "satchel"}
JEWEL_WORDS = {"necklace", "earrings", "earring", "bracelet", "choker", "pendant",
               "ring", "hairpin", "beret", "cap", "hat", "scarf", "gloves"}
# 这些是"戴在头上"的，要单独说清位置，否则会被当包拎着（反之包里东西会被当帽子）
HEAD_WORDS = {"beret", "cap", "hat"}
# 配饰颜色词比服装更宽（gold / silver 之类）
ACC_COLOURS = set(COLOUR_WORDS) | {"gold", "silver", "bronze", "rose", "patent", "leather"}


def _colour_before(ws, i):
    """只认紧挨着的那一个词。

    关键词是"颜色 + 物件"的固定顺序，往前多看几个词反而会串：
    beige knit dress jacket white socks pumps → 按 2 词回溯会把 pumps 说成 white。
    宁可没有颜色（参考图说了算），也不能说错颜色。
    """
    if i >= 1 and ws[i - 1] in ACC_COLOURS:
        return ws[i - 1]
    return ""


def accessory_phrase(kw, half=False):
    """从服装关键词里提取鞋 / 包 / 首饰，单独成句强约束。没有则返回空串。"""
    if not kw:
        return ""
    ws = [w for w in re.split(r"[\s,]+", str(kw).lower()) if w]
    shoes, bags, jews = [], [], []
    for i, w in enumerate(ws):
        lab = ("%s %s" % (_colour_before(ws, i), w)).strip()
        if w in SHOE_WORDS:
            shoes.append(lab)
        elif w in BAG_WORDS:
            bags.append(lab)
        elif w in JEWEL_WORDS:
            jews.append(lab)
    if not (shoes or bags or jews):
        return ""
    bits = []
    if shoes and not half:            # 半身不画脚，鞋那句会逼模型在腰线以下硬画脚
        bits.append("on her feet she wears exactly the %s from the reference - one shoe on each "
                    "foot, same shape, same colour, never barefoot and never a different shoe"
                    % ", ".join(dict.fromkeys(shoes)))
    if bags:
        # 参考图里的包是挂在（无头的）人台袖子上的，本身没有手。
        # 写"提在手里"会逼着模型凭空长出一只手来满足这句话（实测 SPR-0001 举双手时就是这样）。
        # 位置只写"袖子"一个，写"袖子、手臂或肩膀"会同时长出两个包。
        bits.append("there is exactly one %s and it hangs from her sleeve on its own strap, "
                    "not gripped by a hand"
                    % ", ".join(dict.fromkeys(bags)))
    if jews:
        head = [j for j in jews if j.split()[-1] in HEAD_WORDS]
        body = [j for j in jews if j.split()[-1] not in HEAD_WORDS]
        if body:
            bits.append("she wears the %s from the reference, visible and clearly rendered"
                        % ", ".join(dict.fromkeys(body)))
        if head:
            bits.append("she wears the %s from the reference on her head"
                        % ", ".join(dict.fromkeys(head)))
    return ("Accessories must be complete and exactly as shown: " + "; ".join(bits)
            + ". Do not omit any of them, do not replace them with something else, "
              "and do not invent accessories that are not in the reference.")


NEG_TAIL = ("No mannequin, no doll, no plastic or grey body, no ball joints, no headless figure, "
            "no visible outlines, no sketch lines, no drawing, no text, no watermark.")

# 服装参考图里常挂着包（挂在人台的袖子上，本身没有手）。这条无条件加上：
# 否则模型为了"有人拎着包"会在悬空的袖口画一只手，等于凭空长第三条手臂。
# 位置只能写一个（袖子），写"A、B 或 C"会被同时满足，长出两个包。
BAG_RULE = ("If the outfit reference shows a bag or a basket, there is exactly ONE of it and it "
            "hangs from her sleeve on its own strap exactly as in the reference; it is never "
            "gripped by a hand, never duplicated at her other shoulder, and a coat sleeve that "
            "hangs down ends empty with no hand inside it.")

# 半身（腰上取景）专用：骨架对半身裁切会输出纯黑，所以改用"深度图 + 线稿"双参考，
# 并明确告诉模型画面只到腰部、不画腿脚。
HALF_TAIL = ("A real human woman with natural skin, real hair and real eyes is the subject. "
             "Photorealistic waist-up studio portrait cropped at the waist, seamless plain light "
             "studio backdrop, soft even studio lighting, sharp focus, real skin texture, "
             "anatomically correct upper body, exactly two arms and two hands, one arm on each "
             "side of her body, nothing growing out of her sleeves, no third arm and no extra "
             "hand anywhere, the frame ends at her waist so no legs, feet, skirts or trousers "
             "are shown below it.")

# 服装参考图是整身的，但成图只取腰上：加一句说明，避免模型把裙 / 腿 / 鞋也塞进画面。
HALF_COSTUME_NOTE = ("The outfit reference shows the whole outfit, but the final photo is a "
                     "waist-up crop: only the garments above her waist are visible in frame.")

# 半身取景时，体型相关描述必须彻底去掉腿 / 脚 / 大腿 / 臀，否则模型会在腰线以下硬画腿、裙、鞋，
# 使画面从"腰上"漂到"大腿"。这几组与 BODY_PRESETS/BODY_EMPHASIS/BODY_NEG/NO_EXTRA_LIMBS 一一对应，
# 只谈上半身；NO_EXTRA_LIMBS_HALF 只保留手臂 / 手的兜底，删掉所有腿脚约束。
BODY_PRESETS_HALF = {
    "ultra": ("extremely skinny upper body with almost no body fat, very thin waist, "
              "completely flat stomach, visible collarbones and shoulder blades, "
              "small delicate frame, runway editorial skinny silhouette, no curves at all, "
              "paper-thin upper body, flat chest, not a trace of softness"),
    "xxthin": ("very thin slim upper body, narrow waist, completely flat stomach, "
               "visible collarbones, delicate shoulders, small light frame, "
               "very slim idol proportions, no body fat, "
               "paper-thin upper body, no soft flesh, lean and delicate"),
    "slim": ("slim slender upper body, narrow waist, flat stomach, delicate shoulders, "
             "light frame, slim proportions, no extra weight anywhere, "
             "very slim and slender upper body, tight and lean, not a single soft part"),
    "model": ("tall high-fashion model upper body, small waist, narrow elegant silhouette, "
              "willowy slender frame, razor slim torso, no fullness"),
    "normal": ("lean healthy upper body on the slim side, balanced proportions, fit and slim, "
               "neither thin nor heavy, no fullness, "
               "slim and well-proportioned upper body, lean torso, no soft mass"),
    "curvy": ("curvy upper body with a clearly narrow waist, fuller bust, flat stomach, "
              "soft curves without any heaviness, slim-waisted hourglass, "
              "slim and toned everywhere except the bust, no extra fat"),
    "plus": ("fuller plus-size upper body, soft rounded frame, generous curves, "
             "firm and well proportioned, full but firm and toned, still a slim waistline"),
}
BODY_EMPHASIS_HALF = {
    "ultra": "Her upper body is extremely skinny and very thin, with almost no body fat at all, "
              "paper-thin and delicate.",
    "xxthin": "Her upper body is very thin and slim, with a narrow waist and visible collarbones. "
              "No soft flesh anywhere.",
    "slim": "Her upper body is slim, slender and light, tight and lean.",
    "model": "Her upper body is tall, willowy and slim, razor thin.",
    "normal": "Her upper body is lean and slim, well-proportioned.",
    "curvy": "Her upper body is curvy with a slim waist and a flat stomach, but still lean and "
              "toned everywhere except the bust.",
    "plus": "Her upper body is full but firm and toned, with a slim waistline.",
}
BODY_NEG_HALF = {
    "ultra": "No body fat on the upper body, no plump or chubby torso, no thick waist, "
             "no stocky build, no muscular bulk.",
    "xxthin": "No overweight, no plump or chubby torso, no thick waist, no stocky build, "
              "no bulky frame, no belly.",
    "slim": "No overweight, no plump or chubby torso, no thick waist, no stocky build, "
            "no bulky frame, no belly.",
    "model": "No overweight, no plump torso, no thick waist, no stocky build, no bulky frame.",
    "normal": "No obesity, no fullness, no thick waist, no emaciated body.",
    "curvy": "No flat boyish torso, no heavy body, no thick waist, no emaciated frame.",
    "plus": "No extremely thin body, no emaciated figure, no bony frame, no underweight look.",
}
NO_EXTRA_LIMBS_HALF = ("No extra arm, no third arm, no extra hand, "
                       "no disembodied hand gripping a bag, "
                       "no mutated or conjoined anatomy.")
# 半身时服装只认腰以上：删掉裙子 / 裤子 / 腿wear / 鞋，避免模型把下半身塞进画面。
COSTUME_PHRASE_HALF = ("Picture %d shows the exact outfit to wear: reproduce ONLY the garments "
                       "ABOVE the waist exactly as shown - the same tops, outerwear, colour, "
                       "pattern and material - and the same bag. The lower-body garments, "
                       "legwear and shoes are NOT in frame, so do not draw them. "
                       "If the reference has no hat, do not invent a hat. "
                       "The figure in Picture %d is a headless mannequin - do not copy the missing "
                       "head or the mannequin body shape, she has a complete head, neck and face.")


def build_prompt(user, roles, body=DEFAULT_BODY, pose_mode="both", face=DEFAULT_FACE, costume_kw="",
                 height=DEFAULT_HEIGHT, leg=DEFAULT_LEG, bust=50, waist=60, hip=50, half=False):
    key = BODY_ALIAS.get(str(body).strip().lower(), DEFAULT_BODY)
    fkey = FACE_ALIAS.get(str(face).strip().lower(), DEFAULT_FACE)
    parts = []
    if user.strip():
        parts.append(user.strip())
    # 给了真人参考图就以参考图的脸为准，别让族裔描述跟它打架
    if not any(r == "identity" for r, _ in roles):
        parts.append(FACE_PRESETS.get(fkey, FACE_PRESETS[DEFAULT_FACE]))
    # 体型紧跟用户描述，避免被后面的服装/姿势描述稀释
    # 半身只谈上半身，去掉腿/脚/臀，否则画面会从腰上漂到大腿
    body_src = BODY_PRESETS_HALF if half else BODY_PRESETS
    parts.append(body_src.get(key, body_src[DEFAULT_BODY]))
    # 身高 / 三围 / 腿身比：在整体体型之上再雕细节
    fine = []
    # 三围的顺序有讲究：模型对**先出现**的视觉属性权重更高，
    # 所以把胸 / 臀放到最前面（用户最常调这两项），腰和身高靠后。
    try:
        if bust is not None:
            fine.append(bust_desc(float(bust)))
        # 半身不画下半身：臀围/胯部描述会诱导模型把胯和臀画进腰上画面，统统跳过。
        # 腰围要留（画面里能看到腰），但用"腰线"而不是"胯"的说法。
        if hip is not None and not half:
            fine.append(hip_desc(float(hip)))
        if waist is not None:
            fine.append(waist_desc(float(waist)))
    except (TypeError, ValueError):
        pass
    h_desc = HEIGHT_PRESETS.get(str(height).strip().lower())
    if h_desc:
        fine.append(h_desc)
    lg = LEG_PRESETS.get(str(leg).strip().lower())
    if lg and not half:                 # 半身不画腿，腿身比描述会跟腰上取景打架
        fine.append(lg[0])
    if fine:
        parts.append(", ".join(fine) + ".")
    has_skel = any(r == "skeleton" for r, _ in roles)
    for role, idx in roles:
        if role == "skeleton":
            # 骨架必须说清"只是定位图、别画进画面"，否则模型会把彩点棍子画到人身上
            parts.append(
                "Picture %d is a pose guide made of dots and sticks. It is ONLY a positioning map "
                "for the joints and limbs - it is NOT part of the picture: never draw the dots, "
                "the sticks, the colored joints or any skeleton line on her body. Use it solely to "
                "decide where each limb goes; the hands, arms, legs and feet must sit exactly on "
                "those joints. This is the authoritative pose: the other pose pictures must agree "
                "with it, and every limb angle must match these joints exactly - including a leg "
                "raised up to shoulder height, a hand gripping an ankle, a bent knee or an arm "
                "raised overhead." % idx)
        elif role == "depth":
            # 三控制组合（骨架 + 线稿 + 深度）时，深度补的是"体积与前后遮挡"，
            # 明确说清它与骨架/线稿同指同一个姿势，避免模型当成三张不同的图。
            if has_skel:
                parts.append(
                    "Picture %d is a grayscale depth map of the SAME body in the SAME pose: it adds "
                    "the exact 3D volume, which limbs are in front of which, and the depth of the "
                    "hands and feet. It is not a different pose - combine it with the pose guide "
                    "and the line drawing above, all three describe one and the same body. Ignore "
                    "its grey shading completely: do not paint the result grey." % idx)
            else:
                parts.append(
                    "Picture %d is a grayscale depth map of the same body giving its exact 3D volume "
                    "and the depth order of the limbs." % idx)
        elif role == "line":
            if has_skel:
                parts.append(
                    "Picture %d is the SAME pose drawn as line-art: it gives the exact limb "
                    "outlines and joint angles, matching the pose guide above. All of these "
                    "pictures show one and the same pose - reproduce it exactly." % idx)
            else:
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
            if half:
                # 半身取景：服装只认腰以上，明确删掉裙子/裤/腿wear/鞋，否则模型会塞下半身
                parts.append(COSTUME_PHRASE_HALF % (idx, idx))
            else:
                # 注意别在这里列 "socks or tights" 这种"二选一"，模型会把两样都画上：
                # 实测会变成一条腿上叠着丝袜 + 短袜，出现两道袜口。腿部穿着只交给下面那句统一描述。
                parts.append(
                    "Picture %d shows the exact outfit to wear: reproduce every garment, colour, "
                    "pattern and material exactly as shown, including tops, outerwear, skirts or "
                    "trousers, the legwear, the bag and the exact shoe style. "
                    "If the reference has no hat, do not invent a hat. "
                    "The figure in Picture %d is a headless mannequin - do not copy the missing head "
                    "or the mannequin body shape, she has a complete head, neck and face." % (idx, idx))
        elif role == "identity":
            parts.append(
                "Picture %d is the real subject: use her face, facial features, hair colour and "
                "hair style exactly as shown." % idx)
    has_costume = any(r == "costume" for r, _ in roles)
    # 半身取景不画腿脚：腿wear 整段跳过（否则模型会在腰线以下硬画腿/鞋）。
    if not half:
        lw = legwear_phrase(costume_kw)
        if not lw and has_costume:
            lw = ("If the outfit reference shows tights, stockings, socks or leggings on the legs, "
                  "reproduce them exactly - same colour, same transparency, and the same length: they "
                  "must end at exactly the same height as in the reference. An actual garment on her "
                  "body - never omit them, never replace them with bare skin, never lengthen or "
                  "shorten them.")
        if lw:
            parts.append(lw)
    # 鞋 / 包 / 首饰：半身时去掉"鞋在脚上"那句（脚不在画面里），保留包与首饰。
    ac = accessory_phrase(costume_kw, half=half)
    if not ac and has_costume and not half:
        # 自定义上传的服装没有关键词，给条件式兜底
        ac = ("Reproduce every accessory in the outfit reference exactly as shown: the same shoes "
              "on her feet, the same bag carried in her hand, the same jewellery on her body and "
              "the same hat on her head if the reference has one. Never omit them, never swap "
              "them, never invent extra ones.")
    if ac:
        parts.append(ac)
    # 包只给最短的一句、不给位置菜单：写"挂在袖子/手臂/肩膀"会同时长出两个包，
    # 写"提在手里"会长出第三只手。一句话不带选项，实测能只出一个。
    if has_costume:
        if half:
            parts.append(HALF_COSTUME_NOTE)
        parts.append("The bag in the outfit reference hangs from her sleeve.")
    if has_skel:
        parts.append("Rebuild that exact pose as a normal photograph of a real woman with clean "
                     "bare skin: no dots, no markers, no skeleton overlay, no guide lines anywhere.")
    parts.append(HALF_TAIL if half else BASE_TAIL)
    # 半身：强调/负向/肢体兜底都换成只谈上半身的版本，去掉腿脚描述
    if half:
        parts.append(BODY_EMPHASIS_HALF.get(key, BODY_EMPHASIS_HALF[DEFAULT_BODY]))
        parts.append(BODY_NEG_HALF.get(key, BODY_NEG_HALF[DEFAULT_BODY]))
        parts.append(NO_EXTRA_LIMBS_HALF)
    else:
        parts.append(BODY_EMPHASIS.get(key, BODY_EMPHASIS[DEFAULT_BODY]))
        parts.append(BODY_NEG.get(key, BODY_NEG[DEFAULT_BODY]))
        parts.append(NO_EXTRA_LIMBS)
    parts.append(NEG_TAIL)
    return " ".join(parts)


# --------------------------------------------------------------------- 任务
JOBS = {}
JOB_LOCK = threading.Lock()
QUEUE = threading.Semaphore(int(CFG.get("concurrency", 1)))
BATCHES = {}
MAX_BATCH = 20


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


def comfy_free():
    """让 ComfyUI 卸载模型、清显存。连续生成时 12G 卡很容易撞 CUDA fault。"""
    try:
        req = urllib.request.Request(
            C.API.rstrip("/") + "/free",
            data=json.dumps({"unload_models": True, "free_memory": True}).encode(),
            headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=15).read()
        return True
    except Exception:                                          # noqa: BLE001
        return False


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
            leg_k = LEG_PRESETS.get(str(opt.get("leg", DEFAULT_LEG)).strip().lower(),
                                    ("", 1.0))[1]
            job["used_leg"] = round(leg_k, 2)

            has_id = bool(opt.get("identity"))
            half_body = bool(opt.get("halfBody"))
            # 控制图类型（决定挂哪个 refcontrol LoRA）：
            #   "pose"  → 骨架图；  "depth" → 深度图；  None → 不挂控制 LoRA
            # 控制图必须排在参考图的第 1 位（image1），因为 refcontrol LoRA 训练时
            # 控制图就是单独作为第一张输入 + 触发词 refcontrol。
            ctrl_kind = [None]

            def add_pose(src, key, half=False):
                """按模式塞入姿势参考图。参考图总数控制在 4 张以内（含服装/人脸）。"""
                if half:
                    # 半身裁切：骨架必黑，跳过；改用"深度图 + 线稿"双参考控制上半身姿态。
                    # 参考图先裁到腰上，深度图与线稿都只描述腰以上，避免模型把腿画进画面。
                    crop_src = crop_upper_body(src, tmpd, key) or src
                    try:
                        dep = ensure_depth(crop_src, key + "_hb")
                        refs.append(C.stage(dep, "atlas_hb_pd_%s%s" % (key, os.path.splitext(dep)[1])))
                        roles.append(("depth", len(refs)))     # 用 depth 角色，提示词会正确描述成深度图
                        ctrl_kind[0] = "depth"
                        if not has_id:
                            refs.append(C.stage(crop_src, "atlas_hb_ln_%s%s" % (key, os.path.splitext(crop_src)[1])))
                            roles.append(("line", len(refs)))
                        log(job, "half-body: depth+line")
                        return
                    except Exception:                              # noqa: BLE001
                        log(job, "half-body depth failed, fallback to line")
                if abs(warp - 1.0) > 0.01:
                    try:
                        src = slim_warp(src, warp, tmpd, key)
                        key = "%s_w%d" % (key, int(round(warp * 100)))
                        log(job, "slim warp %d%%" % int(round(warp * 100)))
                    except Exception:                              # noqa: BLE001
                        log(job, "slim warp failed")
                if abs(leg_k - 1.0) > 0.01:
                    try:
                        src = stretch_legs(src, leg_k, tmpd, key)
                        key = "%s_l%d" % (key, int(round(leg_k * 100)))
                        log(job, "leg ratio %d%%" % int(round(leg_k * 100)))
                    except Exception:                              # noqa: BLE001
                        log(job, "leg stretch failed")
                if pose_mode == "skeleton":
                    try:
                        sk = ensure_skeleton(src, key)
                        # 判空用"非黑像素占比"，不能用灰度均值：SDPose 输出的是
                        # 黑底彩线骨架，完整人骨架的灰度均值只有 0.0117，
                        # 旧判据(均值<0.1)会把所有正常骨架误判成纯黑，
                        # 导致项目从没用过骨架、一直回退 depth —— 姿态不准的真正根因。
                        if sk and not _skeleton_blank(sk):
                            refs.append(C.stage(sk, "atlas_sk_%s.png" % key))
                            roles.append(("skeleton", len(refs)))
                            ctrl_kind[0] = "pose"          # → 挂 refcontrol pose LoRA
                            # 骨架给出关节坐标，线稿给出轮廓，两者互补。
                            refs.append(C.stage(src, "atlas_ln_%s%s" % (key, os.path.splitext(src)[1])))
                            roles.append(("line", len(refs)))
                            # 再叠一张深度图：补上体积与前后遮挡关系，
                            # 三控制（骨架+线稿+深度）比单骨架更稳，尤其大幅动作。
                            try:
                                dep = ensure_depth(src, key)
                                if dep and os.path.isfile(dep):
                                    refs.append(C.stage(dep, "atlas_pd_%s%s" % (key, os.path.splitext(dep)[1])))
                                    roles.append(("depth", len(refs)))
                                    log(job, "skeleton+line+depth")
                                else:
                                    log(job, "skeleton+line")
                            except Exception:                      # noqa: BLE001
                                log(job, "skeleton+line")
                            return
                        log(job, "skeleton blank -> depth fallback")
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
                        if ctrl_kind[0] is None:
                            ctrl_kind[0] = "depth"       # → 挂 refcontrol depth LoRA
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
                half_body = half_body or bool(is_half_body(src))
                add_pose(src, key, half_body)
                job["used_pose"] = "%s %s" % (p["id"], v["angle"])
            elif custom.get("pose"):
                src = custom["pose"]
                key = "u_%s" % os.path.splitext(os.path.basename(src))[0]
                half_body = half_body or bool(is_half_body(src))
                add_pose(src, key, half_body)
                job["used_pose"] = "自定义"
            if opt.get("costume"):
                c = find_costume(opt["costume"]["id"])
                src = os.path.join(COSTUME_DIR, c["画像"].replace("/", os.sep))
                if half_body:
                    src = crop_upper_body(src, tmpd, "c_%s" % c["id"]) or src
                refs.append(C.stage(src, "atlas_costume_%s%s" % (c["id"], os.path.splitext(src)[1])))
                roles.append(("costume", len(refs)))
                job["used_costume"] = c["id"]
                costume_kw = c.get("キーワード", "")
            elif custom.get("costume"):
                src = custom["costume"]
                if half_body:
                    src = crop_upper_body(src, tmpd, "cu") or src
                refs.append(C.stage(src, "atlas_custom_costume" + os.path.splitext(src)[1]))
                roles.append(("costume", len(refs)))
                job["used_costume"] = "自定义"
            identity_path = None
            identity_ref = None
            if opt.get("identity"):
                identity_path = opt["identity"]["path"]
                # 整张全身照直接当参考等于没给：864x1536 里脸只有几十像素，
                # flux 取不到身份信息。先裁成正脸特写再当 Picture N。
                face_crop = os.path.join(tmpd, "identity_face.png")
                try:
                    os.makedirs(tmpd, exist_ok=True)
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
            # 控制 LoRA（FLUX.2 Klein 的 refcontrol 系列）：训练时是「控制图单独作为
            # image1 + 触发词 refcontrol」，所以要把控制图换到参考图第 1 位。
            # 必须在 build_prompt 之前换 —— 提示词里的 "Picture N" 是按序号引用的，
            # 换完再生成提示词才对得上图。
            ctrl = C.C.get("control_lora") or {}
            loras = []
            if ctrl.get("enabled") and ctrl_kind[0]:
                cl = ctrl.get(ctrl_kind[0]) or {}
                lfile = cl.get("file")
                lpath = os.path.join(C.COMFY, "models", "loras", lfile) if lfile else ""
                if lfile and os.path.isfile(lpath):
                    loras.append((lfile, cl.get("strength", 0.8)))
                    if cl.get("trigger"):
                        opt_prompt = "%s %s" % (cl["trigger"], opt.get("prompt", ""))
                    else:
                        opt_prompt = opt.get("prompt", "")
                    job["used_ctrl_lora"] = "%s@%s" % (lfile, cl.get("strength", 0.8))
                    # 找出控制图（骨架/深度/pose 角色）并挪到第 1 位
                    ci = next((i for i, r in enumerate(roles)
                               if r[0] in ("skeleton", "depth", "pose")), None)
                    if ci is not None and ci != 0:
                        refs.insert(0, refs.pop(ci))
                        roles.insert(0, roles.pop(ci))
                        # 序号整体重排（提示词按序号引用图片）
                        roles = [(r, i + 1) for i, (r, _) in enumerate(roles)]
                        log(job, "control image moved to #1")
            else:
                opt_prompt = opt.get("prompt", "")

            prompt = build_prompt(opt_prompt, roles, opt.get("body", DEFAULT_BODY),
                                  pose_mode, opt.get("face", DEFAULT_FACE), costume_kw,
                                  opt.get("stature", DEFAULT_HEIGHT), opt.get("leg", DEFAULT_LEG),
                                  opt.get("bust", 50), opt.get("waist", 60), opt.get("hip", 50),
                                  half=half_body)
            job["used_body"] = opt.get("body", DEFAULT_BODY)
            job["used_face"] = opt.get("face", DEFAULT_FACE)
            seed = int(opt.get("seed") or 1)
            steps = int(opt.get("steps", 10))

            job["stage"] = "生成中"
            log(job, "graph refs=%d %s" % (len(refs), "x".join(map(str, (w, h)))))
            graph = build_gen(prompt, refs, w, h, seed, steps, job["id"], loras=loras)
            imgs = None
            for attempt in range(3):
                try:
                    imgs = C.submit(graph, verbose=False)
                    break
                except Exception as e:                             # noqa: BLE001
                    if attempt < 2:
                        log(job, "submit retry %d: %s" % (attempt + 1, str(e).splitlines()[0][:70]))
                        comfy_free()
                        time.sleep(4)
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
            # 元数据：成品旁边放一份同名 json，参数全留，日后能追溯/重跑
            meta = {
                "v": 1,
                "job": job["id"],
                "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                "file": os.path.basename(out_final),
                "engine": CFG.get("engine", "FLUX.2 KLEIN"),
                "seed": seed,
                "steps": steps,
                "size": [w, h],
                "elapsed": round(time.time() - job["t0"], 1),
                "swapped": swapped,
                "swapNote": swap_note,
                "batch": job.get("batch"),
                "label": job.get("label"),
                "retriedWithSeed": job.get("seedRetry"),
                "used": {"pose": job.get("used_pose"), "costume": job.get("used_costume"),
                         "body": job.get("used_body"), "face": job.get("used_face"),
                         "warp": job.get("used_warp"), "leg": job.get("used_leg"),
                         "poseMode": pose_mode, "half": half_body,
                         "ctrlLora": job.get("used_ctrl_lora")},
                "prompt": prompt,
                "opt": opt,
            }
            write_meta(out_final, meta)
            job["meta"] = meta
            # 成功就把中间文件清掉；失败才留着便于排查
            shutil.rmtree(tmpd, ignore_errors=True)
            job["state"] = "done"
            job["stage"] = "完成"
            job["elapsed"] = round(time.time() - job["t0"], 1)
            log(job, "done %.1fs" % job["elapsed"])
    except Exception as e:                                         # noqa: BLE001
        msg = str(e)
        traceback.print_exc()
        # 偶发失败自动换种子重跑一次：CUDA fault / 显存不足 / 节点执行异常这类
        # 错误重跑同一个 seed 往往还是失败，换个种子成功率明显更高。
        low = msg.lower()
        retryable = any(k in low for k in ("cuda", "fault", "memory", "out of", "oom",
                                           "execution", "timed out", "connection",
                                           "prompt outputs", "returned no"))
        if retryable and not job.get("retried"):
            job["retried"] = True
            new_seed = random.randint(1, 1000000)
            job["seedRetry"] = new_seed
            log(job, "失败(%s) → 换种子 %d 自动重试"
                % (msg.splitlines()[0][:60], new_seed))
            job["state"] = "queued"
            job["stage"] = "自动重试"
            job["images"] = []
            job["error"] = None
            comfy_free()
            time.sleep(3)
            opt2 = dict(opt)
            opt2["seed"] = new_seed
            run_job(job, opt2)            # 信号量已在 with 退出时释放，不会重入死锁
            return
        job["state"] = "error"
        job["stage"] = "失败"
        job["error"] = msg[:800]
        log(job, "ERROR %s" % e)


# ------------------------------------------------------------------ 批量
def start_batch(opt):
    """把一次批量拆成多个单张任务，串行跑。

    两种模式：
      * mode=costume —— 固定姿势，换 20 套服装
      * mode=pose    —— 固定服装，换 20 个姿态
    串行而不是并发：ComfyUI 那边并发=1，串行能保证出图顺序与队列顺序一致，
    也不会让 20 个线程同时抢信号量。
    """
    mode = str(opt.get("mode") or "costume")
    base = {k: v for k, v in opt.items()
            if k not in ("mode", "costumes", "poses", "seedMode")}
    items = []
    if mode == "costume":
        fixed = opt.get("pose") or None
        if not fixed:
            raise ValueError("固定姿势模式下要先放一个姿势")
        for cid in [str(x) for x in (opt.get("costumes") or [])][:MAX_BATCH]:
            o = dict(base)
            o["pose"] = fixed
            o["costume"] = {"id": cid}
            items.append((cid, o))
    else:
        fixed = opt.get("costume") or None
        if not fixed:
            raise ValueError("固定服装模式下要先放一套服装")
        for ps in [x for x in (opt.get("poses") or []) if x][:MAX_BATCH]:
            o = dict(base)
            o["costume"] = fixed
            ang = ps.get("angle", "front")
            o["pose"] = {"id": ps.get("id"), "angle": ang}
            items.append(("%s @ %s" % (ps.get("id"), ang), o))
    if not items:
        raise ValueError("队列是空的")

    bid = "b" + time.strftime("%y%m%d-%H%M%S-") + uuid.uuid4().hex[:4]
    b = {"id": bid, "mode": mode, "t0": time.time(),
         "seedMode": str(opt.get("seedMode") or "random"), "items": []}
    BATCHES[bid] = b
    for i, (label, o) in enumerate(items, 1):
        job = new_job()
        job["batch"] = bid
        job["label"] = "%d. %s" % (i, label)
        b["items"].append({"job": job["id"], "label": label, "opt": o})
    threading.Thread(target=_run_batch, args=(b,), daemon=True).start()
    return b


def _run_batch(b):
    for it in b["items"]:
        job = JOBS.get(it["job"])
        if not job:
            continue
        job["label"] = "第 %d 张 · %s" % (b["items"].index(it) + 1, it["label"])
        o = dict(it["opt"])
        if b["seedMode"] != "fixed":
            o["seed"] = random.randint(1, 1000000)
        try:
            run_job(job, o)
        except Exception:                                          # noqa: BLE001
            traceback.print_exc()
    b["done_at"] = time.time()


def batch_view(bid):
    b = BATCHES.get(bid)
    if not b:
        return None
    out, done, failed = [], 0, 0
    for it in b["items"]:
        job = JOBS.get(it["job"]) or {}
        st = job.get("state", "queued")
        if st == "done":
            done += 1
        elif st == "error":
            failed += 1
        out.append({"job": it["job"], "label": it["label"], "state": st,
                    "stage": job.get("stage", "排队中"),
                    "label2": job.get("label"),
                    "elapsed": round(time.time() - job["t0"], 1) if job.get("t0") else 0,
                    "images": job.get("images") or [],
                    "error": job.get("error"),
                    "swapped": job.get("swapped"), "swapNote": job.get("swapNote")})
    total = len(b["items"])
    return {"id": b["id"], "mode": b["mode"], "total": total, "done": done,
            "failed": failed, "finished": (done + failed) >= total,
            "elapsed": round(time.time() - b["t0"], 1), "items": out}


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


def stretch_legs(src, k, outdir, tag, waist=0.52):
    """腿身比：把腰线以下纵向拉伸 k 倍，再整体缩回原高。

    拉长腿之后同样画幅里人能"站得更高"、头部相对略小，就是九头身那种感觉。
    腰线用固定比例——线稿人台基本是占满画幅的站姿，够用。
    """
    if abs(k - 1.0) < 0.01:
        return src
    import numpy as np
    from PIL import Image
    im = Image.open(src).convert("RGB")
    W, H = im.size
    y = int(H * waist)
    top = im.crop((0, 0, W, y))
    bot = im.crop((0, y, W, H))
    nh = max(8, int(round(bot.height * k)))
    bot2 = bot.resize((W, nh), Image.LANCZOS)
    canvas = Image.new("RGB", (W, y + nh))
    canvas.paste(top, (0, 0))
    canvas.paste(bot2, (0, y))
    # 缩回原画幅高度，宽度等比缩放，再补边/裁边回到原宽
    nw = max(8, int(round(W * H / canvas.height)))
    canvas = canvas.resize((nw, H), Image.LANCZOS)
    a = np.asarray(im); kk = 8
    corners = np.concatenate([a[:kk, :kk].reshape(-1, 3), a[:kk, -kk:].reshape(-1, 3),
                              a[-kk:, :kk].reshape(-1, 3), a[-kk:, -kk:].reshape(-1, 3)])
    bg = tuple(int(v) for v in np.median(corners, axis=0))
    if nw <= W:
        out = Image.new("RGB", (W, H), bg)
        out.paste(canvas, ((W - nw) // 2, 0))
    else:
        left = (nw - W) // 2
        out = canvas.crop((left, 0, left + W, H))
    dst = os.path.join(outdir, "pose_%s_l%d.png" % (tag, int(round(k * 100))))
    out.save(dst)
    return dst


def ensure_skeleton(src_path, key):
    """线稿人台 → SDPose 全身骨架图（带缓存）。

    SDPose 的 WholeBody 模型能直接在人台灰度图上检出全部关节点（含双手手指、
    双脚脚趾），输出就是 OpenPose 那种"点 + 棍"的骨架。骨架给的是关节坐标而
    不是表面纹理，模型照着摆肢体的准确度比线稿/深度都高：实测"抱膝至胸"、
    "单腿后抬"这类动作，三参考做不出来，加上骨架后能准确还原。

    实测（2026-10-30）：SDPose 对线稿人台的检出质量很高 —— 举起的手搭在头上、
    另一手叉腰、S 形胯部侧顶、双腿分开都能准确还原，连手指/脚趾关键点都有。
    之前姿态不准并不是模型不行，而是 _skeleton_blank 用"灰度均值"判空，
    把黑底彩线的正常骨架全判成纯黑，于是永远走不到 skeleton 分支。

    骨架图必须存 PNG：JPEG 会把细线条压没，黑底上尤其明显。
    """
    # 先查缓存：老版本落的是 jpg（会被压黑），新版本存 png，两种都认
    for ext in (".png", ".jpg"):
        p = os.path.join(SKEL_CACHE, key + "_skel" + ext)
        if os.path.isfile(p) and not _skeleton_blank(p):
            return p
    staged = C.stage(src_path, "skin_%s%s" % (key, os.path.splitext(src_path)[1]))
    # 原生 SDPose 三节点管线（ComfyUI 2026-02 起内置）：比 SDPoseOOD 少一层
    # 旧式封装，且能显式控制 score_threshold 与各类关键点的绘制。
    g = {
        "1": {"class_type": "LoadImage", "inputs": {"image": staged}},
        "10": {"class_type": "CheckpointLoaderSimple",
               "inputs": {"ckpt_name": "sdpose_wholebody_fp16.safetensors"}},
        "11": {"class_type": "SDPoseKeypointExtractor",
               "inputs": {"model": ["10", 0], "vae": ["10", 2],
                          "image": ["1", 0], "batch_size": 4}},
        "12": {"class_type": "SDPoseDrawKeypoints",
               "inputs": {"keypoints": ["11", 0], "draw_body": True, "draw_hands": True,
                          "draw_face": True, "draw_feet": True, "stick_width": 4,
                          "face_point_size": 3, "score_threshold": 0.25,
                          "draw_head": True}},
        "13": {"class_type": "SaveImage",
               "inputs": {"images": ["12", 0], "filename_prefix": "sk_" + key}},
    }
    try:
        imgs = C.submit(g, verbose=False)
    except Exception:                              # noqa: BLE001
        return None
    if not imgs:
        return None
    # 必须用 PNG：骨架是黑底细线，JPEG 一压就糊成一片黑
    saved = C.pull(imgs, SKEL_CACHE, stem=key + "_skel", ext=".png")
    return saved[0] if saved else None


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


def _skeleton_blank(path, min_ratio=0.0005):
    """判断骨架图是否真的"没有检出任何关节"。

    ⚠️ 旧版判据是"灰度均值 < 0.1 就算纯黑"，这是错的：SDPose 输出的骨架图是
    **黑底彩线**，一条完整人骨架的灰度均值只有 0.0117，远低于 0.1，
    于是**所有正常骨架都被误判成纯黑**，项目因此从没用过骨架、一直回退到
    depth 兜底 —— 这才是姿态不准的根因。

    正确判据是"非黑像素占比"：整张纯黑图的非黑占比为 0，而画出骨架后
    即使线条很细也能占到 2% 左右。留 0.05% 余量给极端稀疏的半截骨架。
    """
    try:
        import numpy as np
        from PIL import Image
        im = Image.open(path).convert("RGB")
        a = np.asarray(im)
        ratio = float((a.sum(axis=2) > 30).mean())
        return ratio < min_ratio
    except Exception:                              # noqa: BLE001
        return True


def is_half_body(path, bg_thresh=28):
    """判断参考图是不是半身（腰上）裁切。

    背景差分取人形 → 看人形框底部是不是贴着图像底边、且底部宽度接近肩宽
    （说明是被裁断的躯干而不是到脚的全身）。配合前端的手动开关一起用：
    手动勾选优先，这里只做自动兜底。
    """
    import cv2
    import numpy as np
    from PIL import Image
    try:
        # 用 PIL 读：cv2.imread 在 Windows 上对中文/含非 ASCII 路径会返回 None，
        # 那样会误判成"非半身"又退回骨架黑图。PIL 读图后转 BGR 给 cv2 处理。
        im = Image.open(path).convert("RGB")
        bgr = np.array(im)[:, :, ::-1].copy()
    except Exception:                              # noqa: BLE001
        return False
    H, W = bgr.shape[:2]
    if H < 80 or W < 60:
        return False
    k = max(8, min(24, W // 20))
    corners = np.concatenate([bgr[:k, :k].reshape(-1, 3),
                              bgr[:k, -k:].reshape(-1, 3),
                              bgr[-k:, :k].reshape(-1, 3),
                              bgr[-k:, -k:].reshape(-1, 3)])
    bg = np.median(corners, axis=0)
    diff = np.abs(bgr.astype(np.int16) - bg.astype(np.int16)).max(axis=2)
    m = (diff > bg_thresh).astype(np.uint8)
    m = cv2.morphologyEx(m, cv2.MORPH_OPEN, np.ones((5, 5), np.uint8))
    m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, np.ones((15, 15), np.uint8))
    n, lab, stats, _ = cv2.connectedComponentsWithStats(m, 8)
    if n < 2:
        return False
    i = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
    x0, y0, bw, bh, area = stats[i]
    if area < 0.02 * W * H:
        return False
    bottom_gap = H - (y0 + bh)
    box_ar = bw / float(bh)
    top_band = m[y0:y0 + max(1, int(bh * 0.18)), x0:x0 + bw]
    bot_band = m[y0 + int(bh * 0.85):y0 + bh, x0:x0 + bw] if bh > 12 else m[0:0]
    shoulder = np.count_nonzero(top_band.any(axis=0)) if top_band.size else bw
    bottom_w = np.count_nonzero(bot_band.any(axis=0)) if bot_band.size else 0
    ratio = bottom_w / float(max(1, shoulder))
    # 底部贴底边（被裁断）且底部宽度/肩宽 较大（躯干而非收成腿脚）→ 半身
    return bool((bottom_gap < 0.06 * H) and (ratio > 0.6) and (box_ar > 0.45))


def crop_upper_body(src, outdir, tag):
    """把全身人台/服装图裁到腰上（用"最窄水平切片"定位腰线），用于半身取景时
    让参考图只露上半身，避免模型把腿/裙/鞋也画进画面。裁不出来就返回 None。"""
    import cv2
    import numpy as np
    from PIL import Image
    try:
        # 同 is_half_body：用 PIL 读，避免中文路径下 cv2.imread 直接失败。
        im = Image.open(src).convert("RGB")
        bgr = np.array(im)[:, :, ::-1].copy()
    except Exception:                              # noqa: BLE001
        return None
    H, W = bgr.shape[:2]
    try:
        k = max(8, min(24, W // 20))
        corners = np.concatenate([bgr[:k, :k].reshape(-1, 3),
                                  bgr[:k, -k:].reshape(-1, 3),
                                  bgr[-k:, :k].reshape(-1, 3),
                                  bgr[-k:, -k:].reshape(-1, 3)])
        bg = np.median(corners, axis=0)
        diff = np.abs(bgr.astype(np.int16) - bg.astype(np.int16)).max(axis=2)
        m = (diff > 28).astype(np.uint8)
        m = cv2.morphologyEx(m, cv2.MORPH_OPEN, np.ones((5, 5), np.uint8))
        m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, np.ones((15, 15), np.uint8))
        n, lab, stats, _ = cv2.connectedComponentsWithStats(m, 8)
        if n < 2:
            return None
        i = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
        x0, y0, bw, bh, area = stats[i]
        if area < 0.02 * W * H:
            return None
        # 用"最窄的水平切片"定位腰线：腰是躯干最窄处，腰以下的裙/裤/人台底座都会张开变宽。
        # 这样即使人台带底座、bbox 底部被拉长，也能准确切在腰上，丢掉整片下半身（裙/腿/鞋）。
        sub = m[y0:y0 + bh, x0:x0 + bw]            # 人形区域
        row_w = sub.sum(axis=1)                    # 每行前景宽度
        lo, hi = int(bh * 0.30), int(bh * 0.70)    # 只在中段找，避开头肩过宽与底边底座
        band = row_w[lo:hi]
        if band.size and band.max() > 0:
            waist_row = lo + int(np.argmin(band))
        else:
            waist_row = int(bh * 0.50)
        waist_y = y0 + waist_row
        top = max(0, y0 - int(0.02 * bh))
        # 裁到"腰线偏上"：落在上衣下摆、腰头之上，彻底不露裙/裤/鞋的轮廓，
        # 否则参考图里的腰头会诱导模型在腰线以下接着画下半身。半身取景宁肯少裁一点上衣。
        bottom = max(top + 10, waist_y - max(8, int(0.06 * bh)))
        left = max(0, x0 - int(0.05 * bw))
        right = min(W, x0 + bw + int(0.05 * bw))
        if bottom - top < 10 or right - left < 10:
            return None
        # im 已在函数开头用 PIL 读好（Unicode 安全），直接裁
        crop = im.crop((left, top, right, bottom))
        dst = os.path.join(outdir, "upper_%s%s" % (tag, os.path.splitext(src)[1]))
        crop.save(dst)
        return dst
    except Exception:                              # noqa: BLE001
        return None


def build_gen(prompt, refs, w, h, seed, steps, prefix, loras=None):
    """构建 FLUX.2 Klein 生成图。

    loras: [(lora 文件名, 强度), ...]，串在 UNETLoader → KSampler 之间。
    FLUX.2 的 ControlNet 强化 LoRA（如 refcontrol_v2_poses）必须挂在采样用的
    UNET 上才生效——PainterFluxImageEdit 只是条件编码器，不接受 model 输入。
    """
    g = C.loaders()
    mode = "%d_image" % max(1, len(refs))
    inputs = {"clip": ["2", 0], "vae": ["3", 0], "prompt": prompt, "mode": mode,
              "batch_size": 1, "width": int(w), "height": int(h)}
    for i, r in enumerate(refs, 1):
        nid = str(10 + i)
        g[nid] = {"class_type": "LoadImage", "inputs": {"image": r}}
        inputs["image%d" % i] = [nid, 0]
    g["20"] = {"class_type": "PainterFluxImageEdit", "inputs": inputs}
    # LoRA 链：从 UNETLoader("1") 出发，逐个串到 KSampler
    model_src = "1"
    for i, (lname, lstr) in enumerate(loras or [], start=60):
        g[str(i)] = {"class_type": "LoraLoaderModelOnly",
                     "inputs": {"lora_name": lname, "strength_model": float(lstr),
                                "model": [model_src, 0]}}
        model_src = str(i)
    g["30"] = {"class_type": "KSampler",
               "inputs": C.sampler(model_src, ("20", 0), ("20", 1), ("20", 2), seed, steps)}
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
            if path == "/api/history":
                return self.send_json({"items": history(min(200, int(g("limit", 60))))})
            if path == "/api/presets":
                d = load_presets()
                return self.send_json({"items": [{"name": k, "data": v} for k, v in d.items()]})
            if path.startswith("/api/batch/"):
                v = batch_view(path.rsplit("/", 1)[-1])
                if not v:
                    return self.send_json({"error": "no such batch"}, 404)
                return self.send_json(v)
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
                                           "swapped", "swapNote", "label", "batch",
                                           "seedRetry") if k in job}
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
            if u.path == "/api/batch":
                opt = json.loads(raw.decode("utf-8") or "{}")
                if not C.alive():
                    return self.send_json({"error": "ComfyUI 未启动（127.0.0.1:8188 无响应）"}, 503)
                try:
                    b = start_batch(opt)
                except ValueError as e:
                    return self.send_json({"error": str(e)}, 400)
                return self.send_json({"batch": b["id"], "mode": b["mode"],
                                       "total": len(b["items"]),
                                       "items": [{"job": i["job"], "label": i["label"]}
                                                 for i in b["items"]]})
            if u.path == "/api/presets":
                d = json.loads(raw.decode("utf-8") or "{}")
                act = str(d.get("action") or "save")
                name = str(d.get("name") or "").strip()
                if not name:
                    return self.send_json({"error": "预设名不能为空"}, 400)
                store = load_presets()
                if act == "delete":
                    store.pop(name, None)
                else:
                    data = d.get("data")
                    if not isinstance(data, dict):
                        return self.send_json({"error": "预设内容缺失"}, 400)
                    store[name] = data
                save_presets(store)
                return self.send_json({"ok": True,
                                       "items": [{"name": k, "data": v}
                                                 for k, v in store.items()]})
            return self.send_json({"error": "bad route"}, 404)
        except Exception as e:                                     # noqa: BLE001
            traceback.print_exc()
            return self.send_json({"error": str(e)}, 500)


def backfill_meta():
    """给早期没有 .json 的成品补一份最小元数据，让历史面板能看到它们。

    这些老图参数已经找不回来了，只记时间，标 legacy；前端会隐藏"原样重跑"。
    """
    n = 0
    try:
        days = [d for d in os.listdir(OUTROOT)
                if re.match(r"^\d{4}-\d{2}-\d{2}$", d)]
    except OSError:
        return 0
    for day in days:
        dp = os.path.join(OUTROOT, day)
        try:
            names = os.listdir(dp)
        except OSError:
            continue
        for fn in names:
            if not fn.endswith("_成品.jpg"):
                continue
            img = os.path.join(dp, fn)
            if os.path.isfile(os.path.splitext(img)[0] + ".json"):
                continue
            try:
                mt = time.localtime(os.path.getmtime(img))
            except OSError:
                continue
            meta = {"v": 1, "job": "", "legacy": True,
                    "time": time.strftime("%Y-%m-%d %H:%M:%S", mt),
                    "file": fn, "engine": CFG.get("engine", ""),
                    "seed": None, "size": [], "swapped": None,
                    "used": {}, "opt": {}}
            if write_meta(img, meta):
                n += 1
    return n


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
    n = backfill_meta()
    if n:
        print("  补写历史元数据 %d 张" % n)
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), H)
    print("Atlas Studio -> http://127.0.0.1:%d/" % PORT)
    print("  姿态库 %s | 衣装库 %s" % (POSE_DIR, COSTUME_DIR))
    print("  ComfyUI %s %s" % (C.API, "OK" if C.alive() else "未启动"))
    srv.serve_forever()


if __name__ == "__main__":
    main()
