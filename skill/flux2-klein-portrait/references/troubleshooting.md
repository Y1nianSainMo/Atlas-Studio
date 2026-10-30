# 故障排查

按"症状 → 原因 → 处理"排列，都是实际踩过的坑。

---

## 提交就报 400

**`image1_mask` 类型不对**
把 IMAGE 直接接到 `image1_mask` 会 400。必须先存成灰度 PNG，走
`LoadImage → ImageToMask(channel=red)` 得到 MASK 类型再连。

**节点图字段名拼错**
`/prompt` 的报错信息在 HTTP 响应体里，脚本已经把前 1500 字打出来了，直接看 `node_errors`。

**宽高不是 8 的倍数**
模型能吞，但为了稳妥让脚本按 8 向下取整。

---

## 找不到模型 / 加载失败

- `qwen_3_8b_fp8mixed.safetensors` 不存在，正确的文件是 `qwen_3_8b_fp4mixed.safetensors`（50 系原生支持 fp4）。
- LoRA 下载中断会留下几 MB 的半成品文件，它照样出现在下拉列表里，选中就报错。**半成品直接删掉**，别留在 `models/loras/`。

---

## 人脸换了但脸"不是同一个人"

按顺序排查：

1. **参考图不干净**：一张脸占画面 30% 以上的紧裁图，比半身照效果好得多。用 `faceref.py` 裁。
2. **参考图带了道具**：王冠、面纱、额链会被一起搬过去。裁掉 + prompt 声明裸额头。
3. **LoRA 强度不够**：0.85 以下身份明显漂移，正常用 0.90–1.00。
4. **遮罩太小**：只盖住五官、没盖住发际线与下颌时，脸边缘会有拼贴感。`rx 0.80 / up 1.80 / down 1.30` 是实测好用的比例。
5. **换了景别就漂**：纯参考编辑（不用 LoRA）在全身/远景下必然漂眼型、脸宽。要严格锁人只能上换脸 LoRA 或 InstantID/PuLID 一类的方案。

---

## 脸和脖子两个色号

`ColorMatch(image_ref=原图, image_target=结果, strength 0.35)` 就能压住。
强度 0.5 以上会把整体色调拉偏，别加太狠。prompt 里补一句
`match the skin tone and shadow density of Picture 1` 也有帮助。

---

## 额头长出饰品 / 发型被换掉

参考图里的物品会通过图像条件泄漏。必须**同时**做两件事：

1. 参考图裁到不含该物品（`faceref.py --top 0.15` 往上缩）
2. prompt 写 `her forehead is completely bare and clean, no crown, no headpiece, no jewellery`
   + `keep the hairstyle and hair colour of Picture 1`

只做其中一件通常不够。

---

## 说"特写"但输出还是全身

根因不在 prompt，在 `image1` 的构图。**参考图的景别决定输出景别**。
换一张头部裁切图当参考，再补 `CAMERA FRAMING: CLOSE-UP, head and shoulders only`。

---

## 脚部/鞋子出问题

见 `prompts.md` 第 5 节。要点：换结构明确的鞋型（closed back + 踝带），
加"恰好两只鞋"的硬约束，最后用 `inpaint.py --zoom 2.2` 局部收尾。
单纯放大重绘、不改鞋型，通常还是错。

---

## 皮肤发白、有白斑点、假人感

提示词里删掉 `punchy contrast / visible pores / no retouching`，
改成 `soft flat overcast daylight, lifted shadows` +
`clean smooth natural skin, no white spots` +
`no beautification, do not slim the chin, do not enlarge the eyes`。

---

## 下载模型极慢或断流

`huggingface.co` 走 `hf-mirror.com`，单线程只有 60 KB/s。
用 `scripts/fast_dl.py <url> <dst> 10`，10 线程分段 Range 下载能到 400–700 KB/s，
支持断点续传（先删掉旧的 `.part` 若要重下）。

---

## 服务没起

```
E:/ComfyUI_Mie_V33/1_1点击启动comfyui.bat
```

`comfy.py` 的 `require_server()` 会直接报这个路径。跑批量前先确认
`curl http://127.0.0.1:8188/system_stats` 有返回。

---

## 换脸跑完几乎没变化（脸没被替换）

**症状**：`pass2 ok`、耗时也对，但成品和原图的脸几乎一样。

**原因**：`headmask.make_one` 用 Haar 在**整图**上找最大人脸，全身棚拍图里常误检到
腿部/裙摆。实测 832×1216 的全身图，遮罩 bbox 落在 `y=805~1216`（腿的位置），
头部完全没盖住，LoRA 自然无从下手。

**排查**：看 `ComfyUI/input/hsm_*.png`，用 `PIL.Image.getbbox()` 打在原图上叠一层，
红区盖的是腿就中招了。

**修法**（`atlas-studio/studio_server.py` 的 `smart_head_mask` 已实现）：
1. 背景差分（四角取中位色）取人形最大连通域，拿到身体框 `y0/h`
2. Haar 只在顶部 55% 找脸，并**校验** `y < y0 + 0.25h`，否则丢弃
3. 找不到就按人形轮廓推：`cx = 腰线中点`，`cy = y0 + 0.075h`，`rx = 0.062h`，`ry = 0.10h`
4. 椭圆 ×(0.95w, 1.25h) 扩张后 blur 22

棚拍纯色背景场景下两条路径都稳，人台图（无脸）走第 3 条也能盖住头。
