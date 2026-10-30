# Atlas Studio · 拖放生图台

姿态库 + 衣装库 → 一键出棚拍级人像。拖一张姿势、拖一套衣服、选好身材参数，点生成。

- **打包日期**：2026-10-30
- **生成引擎**：FLUX.2 Klein 9B（含 SDPose 骨架姿态控制、换脸 LoRA）
- **数据规模**：姿态 288 组 × 5 机位 = 1439 张；衣装 1407 套 × 3 档
- **本包体积**：约 2.9 GB（含两个素材库，不含模型权重）

---

## 目录结构

```
Atlas-Studio-2026-10-30/
├── app/                        应用本体
│   ├── studio_server.py        后端（标准库 http.server，无额外依赖）
│   ├── index.html              前端（单文件，深色界面）
│   ├── config.json             配置（路径已改为相对，整包可搬）
│   └── 启动 Atlas Studio.bat   一键启动
├── data/
│   ├── pose-atlas/             RuiC-PoseAtlas 姿态库（1439 张线稿人台）
│   └── costume-gallery/        衣装库（1407 套，thumbs/img/png 三档）
├── skill/
│   └── flux2-klein-portrait/   ComfyUI 节点图脚本库（comfy.py / faceswap.py 等）
├── docs/
│   ├── 01-依赖与节点清单.md     ★ ComfyUI 需要装哪些节点
│   ├── 02-模型清单.md           ★ 需要哪些模型文件、放哪个目录
│   ├── 03-部署与迁移.md         换机器怎么装、路径怎么改
│   └── 04-功能与参数说明.md     界面每个参数是什么意思
└── versions/
    └── 2026-10-29-v1/          上一稳定版备份（可直接回滚）
```

---

## 快速开始

1. **启动 ComfyUI**：确认 `http://127.0.0.1:8188` 能打开（模型和节点见 docs/01、docs/02）
2. **双击** `app\启动 Atlas Studio.bat`
3. 浏览器自动打开 `http://127.0.0.1:8125/`
4. 拖姿势到「01」、拖服装到「02」、（可选）拖真人照到「03」，点「生成」

> 「Atlas Server」那个黑窗口不要关，关了服务就停了。

---

## 出图存在哪

```
app/work/outputs/<日期>/21-33-12_成品.jpg
```

按日期分文件夹，文件名是「时-分-秒_成品」。换脸时会多一张 `_底图.jpg`（未换脸的版本）。

中间文件（人脸裁剪、遮罩、深度图、骨架图）都在 `app/work/tmp`、`work/depth`、`work/pose`，不会混进出图目录。

---

## 常见问题

| 现象 | 处理 |
|---|---|
| 页面一直「检测中」 | 服务没起来，看 bat 窗口报错；或端口 8125 被占 |
| 「ComfyUI 未启动」 | 单独启动 ComfyUI，确认 8188 可访问 |
| 生成报 CUDA fault | 12G 显存吃紧，程序会自动清显存并重试 2 次；仍失败就关掉其它占显存程序 |
| 换了机器跑不起来 | 见 docs/03，主要是 bat 里的 Python 路径和 skill/config.json 里的 ComfyUI 路径 |
