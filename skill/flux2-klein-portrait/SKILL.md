---
name: flux2-klein-portrait
description: 用本机 ComfyUI + FLUX.2 Klein 9B 跑图，覆盖文生图、多参考图编辑、姿态图集控姿势、扩图、局部重绘（去水印/修鞋/修手）和换脸 LoRA 锁人。当用户要求"用本地工作流生图""flux-2-klein 跑""同一个人的系列图""把这张脸换到那几张图上""按这个姿势/人台姿势生成""来一组不同姿势""扩图/补边""去掉水印""把脚修一下""棚拍/特写/换姿势"，或提到 ComfyUI、PainterFluxImageEdit、RuiC-PoseAtlas、姿态图鉴、换脸 LoRA、klein 工作流时使用。
agent_created: true
---

# FLUX.2 Klein 本地人像生成

一套在本机 ComfyUI 上跑 FLUX.2 Klein 9B 的标准流程：从参考图生成同人系列、扩图、
局部重绘、换脸。脚本已经把这套节点图、遮罩策略和踩坑处理固化下来，不要重新手写节点 JSON。

## 使用前提

- ComfyUI 已启动：`E:/ComfyUI_Mie_V33/1_1点击启动comfyui.bat`，API `127.0.0.1:8188`
- 跑脚本用 `E:/ComfyUI_Mie_V33/python_embeded/python.exe`（自带 PIL / cv2），系统 Python 通常没有
- 路径、模型名、LoRA 名都在 `config.json`，换机器只改这里
- 动手前先 `curl -s http://127.0.0.1:8188/system_stats`，没返回就先让用户启动

## 标准流程

### 第 0 步：判断任务类型

| 用户说法 | 走哪条 |
|---|---|
| "按这几张生成同风格的 N 张""换姿势/换场景" | 文生图 / 参考图编辑 → `gen.py` |
| "要这个姿势""按这张人台的姿势来""来一组不同姿势" | 姿态图集 → `pose.py` + `gen.py` |
| "比例不对，扩一下""补边" | 扩图 → `outpaint.py` |
| "去掉水印""把鞋/脚修好""手改一下" | 局部重绘 → `inpaint.py` |
| "把这张脸换到那几张上""统一成同一张脸" | 换脸 → `faceref.py` + `faceswap.py` |

### 第 1 步：先定景别，再写 prompt

**参考图的构图决定输出构图，prompt 改不动景别。** 要特写就用头部裁切图当参考，
要中景就用"头到大腿"的裁切图。这一条能省掉大量无效重跑。

人物描述段整段复用，只改动作、景别、机位，保证系列图是同一张脸。

**要指定姿势就别用文字描述姿势**，走姿态图集：`pose.py` 从 288 组人台线稿里
按关键词取图，作为 Picture 1；身份照作为 Picture 2。实测姿势一致率远高于文字描述。

```
python scripts/pose.py search -q 站姿 --sheet pick.jpg      # 先看缩略图挑
python scripts/pose.py jobs -q 坐姿 --limit 6 --identity me.jpg --out jobs.json
python scripts/gen.py --jobs jobs.json --out ./out
```

### 第 2 步：跑批量

```
# 文生图 / 参考图编辑
python scripts/gen.py --prompt "..." --ref a.jpg --ref b.jpg --size 864x1536 --seed 21 --out ./out

# 批量（jobs.json 里每条含 prompt / seed / prefix / refs / size / steps）
python scripts/gen.py --jobs jobs.json --out ./out
```

### 第 3 步：必检

跑完一定要出联络表并看图，不要直接交付：

```
python scripts/sheet.py --in ./out/*.jpg --out ./out/_sheet.jpg
python scripts/sheet.py --in ./out/*.jpg --face-grid --out ./out/_faces.jpg   # 系列图查身份一致性
```

逐项检查：构图是否被裁脚、手指数量、鞋子是否穿反/空壳/三只、脸部接缝与色差、
额头有没有多出饰品。发现问题用 `inpaint.py --zoom` 单张修，不要整批重跑。

## 四类任务的要点

### 姿态图集（pose.py）

本地 clone 在 `E:/RuiC-PoseAtlas`（288 姿态 / 1439 张 900×1200 人台线稿，正/斜侧/侧/俯/仰）。
`pose.py` 检索→取图→生成 jobs.json，一条命令出整批不同姿势。

- 线稿当 Picture 1 只传姿势和机位，模型会渲染成真人照片，**不要反相、不要描黑**
- 约 1/4 概率会"把线稿画进去"：出现灰色人台、半透明叠影、双重轮廓 → 换 seed 重跑那几张
- 默认 `--size auto` 跟随 3:4，改比例会跟着改取景
- 人台没有脸和脚型细节，脚部照样要按第 3 步查

### 文生图 / 参考图编辑（gen.py）

- `mode` 随 `--ref` 数量自动切换；不给 `--ref` 就是纯文生图（已验证可用）
- 采样固定 `steps 8 / cfg 1.0 / euler / simple / denoise 1.0`，不要改
- 蒸馏模型 cfg 恒为 1.0，负面词基本无效，要"不要 X"就改写成正向描述

### 扩图（outpaint.py）

- 画布尺寸必须自己算：`宽 = 原宽 + left + right`，脚本内部已处理
- `--pad 128,48,128,48` 这类对称补边能保住原比例；ColorMatch 默认 0.4，别关

### 局部重绘（inpaint.py）

- 遮罩必须是 MASK：走 `ImageToMask(red)`，脚本已封装
- 去水印：prompt 只写"这里应该是什么"（`clean sand, smooth fabric`），别提水印
- 小缺陷（鞋、脚、手指）**一定用 `--zoom 2.2`**：裁区域 → 放大 → 重绘 → 羽化贴回

```
python scripts/inpaint.py --in shot.jpg --box 0.32,0.86,0.68,0.99 --zoom 2.2 \
  --prompt "black patent leather pointed-toe pumps, closed back, exactly two shoes" --seed 5 --out ./out
```

### 换脸（faceref.py + faceswap.py）

```
# 1. 从参考人像裁出身份脸（有额饰/头链时用 --from-eyes + 小 --sides）
python scripts/faceref.py --in desert.jpg --out ref_face.png --from-eyes 0.30 --sides 0.04 --stage

# 2. 先看一眼遮罩对不对，再等整批
python scripts/headmask.py --in shots/*.jpg --debug ./out/_maskcheck

# 3. 批量换
python scripts/faceswap.py --target shots/*.jpg --face ref_face.png --strength 0.95 --out ./out
```

- Image1 = 目标场景，Image2 = 身份脸；遮罩只盖头部，别盖到锁骨和衣领
- LoRA 强度：0.95 端正，0.88–0.90 皮肤更真实但偶发左右眼不对称；近景用 0.95
- 参考图上的饰品会被搬过去，必须裁掉 + prompt 声明 `forehead is completely bare`
- 瞳色要显式写（`light grey-blue eye colour`），否则会回到棕黑

## 脚本清单

| 脚本 | 作用 |
|---|---|
| `comfy.py` | API 客户端、模型加载器、节点图零件、stage/pull/提交等待。其他脚本都 import 它 |
| `gen.py` | 文生图 / 1–8 张参考图编辑 |
| `outpaint.py` | 扩图（ImagePadForOutpaint + ReferenceLatent + ColorMatch） |
| `inpaint.py` | 整图重绘 / 遮罩重绘 / `--zoom` 放大重绘贴回 |
| `headmask.py` | Haar 检测 → 椭圆头部遮罩 → 暂存到 ComfyUI/input + 调试图 |
| `faceref.py` | 裁身份参考脸，可避开额前饰品 |
| `faceswap.py` | 换脸 LoRA 批量，含 ColorMatch |
| `pose.py` | RuiC-PoseAtlas 姿态图集：检索 / 取图 / 批量生成 jobs.json |
| `sheet.py` | 联络表 / 人脸拼图 |
| `fast_dl.py` | 大文件多线程 Range 下载，hf-mirror 慢速时用 |

## 铁律

1. 不重写节点图。所有图都从 `comfy.py` 的 `loaders()` / `sampler()` 起，改 prompt 和参数。
2. 采样参数不调。`steps 8 / cfg 1.0 / euler / simple / denoise 1.0`，挂 LoRA 用 4 步。
3. 景别不对先换参考图，不要反复改 prompt。
4. 交付前必须看联络表；有瑕疵单张 `--zoom` 修，不整批重跑。
5. 用户说"严格保持人物不变"时，明确告知：纯参考编辑会漂，只有换脸 LoRA 能压到可用程度，但仍非 100%。

## 参考文档

- `references/workflows.md` — 节点图、参数、模型、性能基准
- `references/prompts.md` — 提示词库（构图/棚拍/皮肤/鞋/换脸/扩图/去水印）
- `references/poses.md` — 姿态图集用法、检索语法、已知翻车与修法
- `references/troubleshooting.md` — 症状 → 原因 → 处理
