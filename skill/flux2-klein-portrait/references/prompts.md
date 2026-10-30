# 提示词库（实战验证过）

写法原则：**用正向陈述描述画面，而不是堆负面词**。cfg=1.0 时负条件几乎不起作用，
"不要 X" 要改写成"Y 是什么样"。

---

## 1. 人物一致性（同一个人、不同场景）

把身份描述写成一句可复用的断言，每次生成都带上：

```
the same young Chinese woman as the reference photos, identical face, identical hairstyle
```

换姿势/换景不要重写整段，只改景别与动作，人物描述逐字复用。

---

## 2. 构图与景别（最容易翻车的地方）

**取景由 image1 决定，prompt 改不动。** 要特写就换头部裁切图做参考，
要中景就换"头到大腿"的裁切图。prompt 只用来加固，防止模型自己拉远：

```
CAMERA FRAMING: MEDIUM SHOT. Her legs are OUTSIDE the bottom of the frame and NOT visible.
```

```
CAMERA FRAMING: CLOSE-UP portrait, head and shoulders only, the frame ends at the collarbone.
```

居中和留白：

```
full body centered in frame, generous empty space above the head and below the shoes,
entire body from head to feet inside the frame, nothing cropped
```

---

## 3. 棚拍 / 纯色背景

```
professional studio photograph against a plain seamless light grey backdrop,
soft even studio lighting, no visible light stands, no equipment, no props,
clean minimal background, subtle shadow under her feet
```

要去掉"灯光/器材"就别提它们的存在，只描述"plain seamless backdrop"。
写了 "studio lighting" 容易顺带生成灯架，所以额外补一句 `no light stands, no equipment`。

---

## 4. 皮肤质感与对比度

默认容易过曝、发白、磨皮假人。这组词是调节旋钮：

| 想要 | 写 |
|---|---|
| 降低对比、柔光 | `soft flat overcast daylight, lifted shadows, low contrast` |
| 恢复真实皮肤 | `clean smooth natural skin, natural pores, no white spots, no blown highlights` |
| 禁止美颜 | `no beautification, do not slim the chin, do not enlarge the eyes, no makeup retouch` |
| 加胶片感 | `gentle film grain, 35mm lens, shallow depth of field` |

反过来，`punchy contrast, visible pores, no retouching` 这类词会把皮肤推成橘皮 + 白斑点，慎用。

---

## 5. 鞋子与脚（高频缺陷）

模型在全身图里最容易出错：露脚跟、空壳鞋露出内底、三只鞋、脚没穿鞋、光脚。
**改 prompt 收效有限，最有效的是把鞋型换成结构明确的款式**：

```
black patent leather pointed-toe stiletto pumps with a closed back and a thin black ankle strap
```

再加三条硬约束：

```
the back counter is closed and wraps the heel, no pale insole visible, no open back,
exactly two shoes, one worn on each foot, no third shoe, no duplicated leg
```

仍有问题时，用 `inpaint.py --zoom 2.2` 只重绘脚部区域，prompt 里重复上面这段鞋型描述。

---

## 6. 换脸（identity transfer）

模板（`{T}` = 触发词 `jhuangswap`）：

```
{T}, transfer the face of the woman in Picture 2 onto the woman in Picture 1.
Keep the pose, body, clothing, background, lighting and framing of Picture 1 exactly.
Preserve her facial bone structure, eye shape, eye colour, eyebrows, nose and lips from Picture 2.
Her forehead is completely bare and clean, with no crown, no headpiece, no jewellery, no forehead ornament;
her hair is simply styled as in Picture 1.
Keep the hairstyle and hair colour of Picture 1.
Match the skin tone and shadow density of Picture 1, blend the neck and jawline naturally,
photorealistic retouch, natural skin texture.
```

要点：

- **瞳色要显式写出来**。只写 `eye colour` 会被拉回棕黑；写 `light grey-blue eye colour` 才保得住。
- **参考图上的饰品会被一起搬过去**（王冠、额链、发簪）。两个动作一起做才干净：参考图裁掉饰品 + prompt 声明 `forehead is completely bare`。
- 保头发写 `keep the hairstyle and hair colour of Picture 1`，否则会照抄参考图的发型。
- 想要更真实（毛孔、颗粒）就把 LoRA 降到 0.88–0.90 并加 `natural pores, subtle grain, not airbrushed`；代价是偶发左右眼不对称。近景/成片优先用 0.95 求端正。

---

## 7. 扩图

prompt 只描述**延续出去的部分**，并声明整体不变：

```
extend the scene seamlessly to the left and right,
the same film still, the same lighting and colour grade, continuous background,
no new subject, no border, no frame
```

---

## 8. 去水印

```
clean smooth sand, natural dune texture, consistent lighting and grain
```

不要写 "remove watermark"——写"这里应该是什么"，模型才会把水印当成噪声抹掉。

---

## 9. 批量做系列图的做法

一次任务里每个姿势/景别写一条 prompt，人物描述段复制粘贴保持一致，只改：

1. 动作（arms crossed / hand on hip / seated on the floor / walking）
2. 景别（full body / medium / close-up）
3. 机位（front / three-quarter / over the shoulder）

seed 用 `基数 + 序号`，便于单独重跑失败的那一张。
