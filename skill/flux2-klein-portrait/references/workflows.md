# 工作流与参数（FLUX.2 Klein 本地部署）

## 环境

| 项 | 值 |
|---|---|
| ComfyUI 根目录 | `E:/ComfyUI_Mie_V33/ComfyUI` |
| 启动 | `E:/ComfyUI_Mie_V33/1_1点击启动comfyui.bat` |
| API | `http://127.0.0.1:8188`（`/prompt` 提交，`/history/<id>` 取结果） |
| 推荐 Python | `E:/ComfyUI_Mie_V33/python_embeded/python.exe`（自带 PIL / cv2 / numpy） |
| 输入目录 | `ComfyUI/input`（`LoadImage` 只能读这里，脚本会先 stage 拷贝） |
| 输出目录 | `ComfyUI/output` |
| GPU | RTX 5070 12GB，显存够跑 9B fp8 + LoRA |

模型三件套（`models/`）：

| 文件 | 大小 | 说明 |
|---|---|---|
| `flux-2-klein-9b-fp8.safetensors` | 9.4 GB | UNET，蒸馏版 |
| `qwen_3_8b_fp4mixed.safetensors` | 6.8 GB | CLIP/文本编码器。**不存在 fp8mixed 版本**，别照抄别人的配置 |
| `flux2-vae.safetensors` | 336 MB | VAE |

关键自定义节点：`PainterFluxImageEdit`（包 `Comfyui-PainterFluxImageEdit`）

```
输入: clip, vae, prompt, mode(1_image..8_image), batch_size, width, height,
      image1..image10, image1_mask(MASK)
输出: positive(CONDITIONING), negative(CONDITIONING), latent(LATENT)
```

它一次性产出正负条件与 latent，直接接 `KSampler`，不需要再写 CLIPTextEncode。
`mode` 的数字就是参考图数量；**mode=1_image 且不给 image1 时等于纯文生图**（已验证）。

## 采样参数

蒸馏模型，固定这一套：

```
steps 8（挂 LoRA 用 4） / cfg 1.0 / euler / simple / denoise 1.0
```

cfg 保持 1.0：节点自带的负条件基本不生效，别指望靠负面词救图，要写在正向描述里。

## 四张图

### 1. 文生图 / 参考图编辑（gen.py）

```
UNETLoader ─ CLIPLoader ─ VAELoader
LoadImage(image1..N) ─┐
                      ├─ PainterFluxImageEdit(mode=N_image, width, height)
                      └─ → KSampler → VAEDecode → SaveImage
```

- 参考图数量决定取景、比例、构图 —— **image1 的构图就是输出的构图**，不是靠 prompt 里的 "medium shot" 改的。
- 想要特写/中景，换一张对应景别的参考图，比改一百遍 prompt 有效。

### 2. 扩图（outpaint.py）

```
LoadImage → ImagePadForOutpaint(left,top,right,bottom, feathering=32)
          → VAEEncode → ReferenceLatent(正/负各一次)
          → EmptyFlux2LatentImage(补齐后的宽高) → KSampler
          → VAEDecode → ColorMatch(ref=pad后的图, strength 0.4) → SaveImage
```

- 画布尺寸必须自己算：`w = 原宽 + left + right`，不能沿用原尺寸。
- 例：1920×704 的图补 `128,48,128,48` → 2176×800，比例仍是 2.72:1。
- ColorMatch 让新生成的边缘与原图色调对齐，不做会出现明显色差带。

### 3. 局部重绘 / 去水印（inpaint.py）

```
LoadImage(img) ─ LoadImage(mask.png) ─ ImageToMask(channel=red) → image1_mask
PainterFluxImageEdit(mode=1_image, image1, image1_mask) → KSampler → VAEDecode → SaveImage
```

- 遮罩必须是 **MASK 类型**：白底黑字的灰度 PNG 走 `ImageToMask(red)`。直接接 IMAGE 会 400。
- 去水印：遮罩略大于水印，羽化 18–24，prompt 只描述该区域应该长什么样（"clean sand, smooth fabric"），不要提"水印"。
- 小面积缺陷（鞋、脚、手指）用 `--zoom 2.2`：裁出该区域 → 放大 → 重绘 → 羽化贴回。原分辨率不变，模型能看到足够细节。

### 4. 换脸（faceswap.py）

```
UNETLoader → LoraLoaderModelOnly(strength_model)   ← 换脸 LoRA
LoadImage(target) + LoadImage(headmask) → ImageToMask(red) → image1_mask
LoadImage(face_ref) → image2
PainterFluxImageEdit(mode=2_image, width/height = 目标尺寸) → KSampler(steps 4)
→ VAEDecode → ColorMatch(ref=原图, strength 0.35) → SaveImage
```

- 触发词 `jhuangswap`，LoRA 强度 0.85–1.0，4 步就够。
- Image1 = 目标场景，Image2 = 身份参考。
- 支持遮挡感知：手压在脸上、头发盖住耳朵都能处理。
- 头部遮罩：椭圆 + 高斯羽化（`rx 0.80 / up 1.80 / down 1.30 / blur 22`，均相对人脸框）。遮罩过大（盖到锁骨、衣领）会把衣服一起重绘。
- ColorMatch 0.35 用来压住"换上的脸比脖子白"。

## 性能基准（864×1536）

| 任务 | 步数 | 单张耗时 |
|---|---|---|
| 文生图 / 双参考编辑 | 8 | 45–56 s |
| 换脸（LoRA） | 4 | 24–32 s |
| 扩图 2176×800 | 8 | 27–33 s |
| 局部重绘（zoom 2.2 小区域） | 8 | 20–30 s |
