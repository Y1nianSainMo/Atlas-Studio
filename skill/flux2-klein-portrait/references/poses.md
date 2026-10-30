# 姿态图集（RuiC-PoseAtlas）用法

## 是什么

[RuiC-PoseAtlas](https://github.com/HRuiCcc/RuiC-PoseAtlas) 是静态人体姿态图库：288 组姿态、
4–5 个机位（正面 `front` / 斜侧 `diagonal` / 侧面 `side` / 俯视 `high` / 仰视 `low`），
共 1439 张 **900×1200 灰线人台** PNG，白底、无脸、球形关节。原始素材来自 POSE-DB-01（作者 ギア）。

对人像生成的用处：**人台线稿当 Picture 1 = 免费的姿势 ControlNet**。模型会丢掉线稿风格、
渲染成真人照片，同时严格保留四肢走向和机位。合作身份照一起用就是"同一个人换姿势"。

## 数据位置

| 用途 | 路径 |
|---|---|
| 本地仓库（优先） | `E:/RuiC-PoseAtlas`（641 MB img + 26 MB thumbs + 204 KB data） |
| 线上演示站（回落） | `https://hruiccc.github.io/RuiC-PoseAtlas` |
| 技能内缓存 | `<skill>/pose_atlas/`（poses.json ＋ 按需取到的图） |

`scripts/pose.py` 的顺序：`POSE_ATLAS_BASE` 环境变量 → `config.json` 的 `pose_atlas.base`
→ 该目录存在就用本地，不存在自动回落到线上。所以 clone 删了也不会坏。

本地浏览（教程做法）：

```
cd E:/RuiC-PoseAtlas
E:/ComfyUI_Mie_V33/python_embeded/python.exe -m http.server 8123 --bind 127.0.0.1
# http://127.0.0.1:8123/
```

深链：`http://127.0.0.1:8123/?lang=en#pose=full_single_P019&angle=side`

## 检索语法

```
python scripts/pose.py search -q 站姿                       # 关键词（中/英/日/编号）
python scripts/pose.py search -q 坐姿 --series full_sexy     # 系列
python scripts/pose.py search --category 跪立 --angle side   # 分类 + 机位
python scripts/pose.py search --ids full_std_P015,P016      # 精确 id
python scripts/pose.py search -q 站姿 --limit 6 --sheet pick.jpg   # 出缩略图挑选
```

- `--series`：`full_sexy` / `full_std` / `upper` / `duo`
- `--category`：站姿、坐姿、跪立・四肢着地、趴卧、特殊、背向坐位…（26 个）
- `--angle`：front / side / diagonal / high / low / all（也吃中文：正面、侧面、斜侧、俯视、仰视）
- 只给编号（如 `P019`）会命中**所有系列**的同号姿态，要唯一就写全 `full_std_P019`
- 索引缓存 30 天；换版本或怀疑过期用 `pose.py index --refresh`

## 三条命令跑完整批

```
# 1. 挑：先出缩略图看，别凭名字选
python scripts/pose.py search -q 站姿 --series full_std --limit 12 --sheet pick.jpg

# 2. 组：姿态图 + 身份照 → jobs.json（尺寸自动跟随 3:4，默认长边 1024）
python scripts/pose.py jobs --ids full_std_P015,full_std_P016,full_std_P017 \
  --identity E:/path/me.jpg --out jobs.json

# 3. 跑
python scripts/gen.py --jobs jobs.json --out ./out
```

`jobs` 的其它开关：`--angle front,side`（多机位）、`--angle all`（同姿态全机位）、
`--long-side 1200`（出大图）、`--ref-dir`（姿态图落盘位置，默认 `./pose_refs`）、
`--template`/`--prompt`（自定义模板文件）、`--seed`（起始种子）。

模板占位符：`{pose}` `{pose_en}` `{pose_ja}` `{id}` `{no}` `{category}` `{angle}` `{angle_zh}` `{angle_en}`。

单张取图：

```
python scripts/pose.py get --ids full_sexy_P001 --angle side --out ref/p001_side.png [--stage] [--size 768x1024]
```

`--stage` 会顺手复制进 ComfyUI/input，方便直接塞进网页端工作流。

## 生成的提示词要点

默认模板（`pose.py` 内置）已经写好这三句，改模板时别丢：

1. `Picture 1 is a grey line-art mannequin drawing that ONLY defines the body pose,
   limb positions and camera framing - ignore its drawing style completely.`
   —— 不给这句，模型会把线稿风格一起搬过来。
2. `Picture 2 defines the woman's face, hair, body type and outfit`
   —— 身份照必须显式声明为"主体来源"，否则会和线稿打架。
3. `exactly two legs and two feet` + `Real skin texture, natural anatomy`
   —— 人台没有鞋和脚趾，这句能压住多腿/畸形脚。

补充写法：姿势本身用 `{pose_en}` 自动带出（如 `standing, contrapposto, hand on hip`），
比手写 "one hand on her hip" 更贴合线稿。

## 已知翻车与修法

| 现象 | 原因 | 修法 |
|---|---|---|
| 输出里出现灰色人台 / 黑胶衣机器人 | 线稿风格被当成主体，和 identity 抢 | 换 seed 重跑；prompt 加 `no mannequin, no doll, no plastic or grey body, no ball joints` |
| 半透明线稿叠影、双重轮廓 | 同上，1/4 概率 | 换 seed；同一姿态多出 2 个种子挑一张 |
| 姿势被拉回正面 | 线稿本身就是正面站姿，或 prompt 里写了别的动作 | 换 `--angle side/diagonal`；prompt 不要重复描述动作 |
| 剪影化预处理后衣服变成灰块 | 把线稿填成实心灰会把"平涂"风格带过去 | **不要做剪影预处理**，直接用原始线稿 |
| 脚部畸形 / 三只脚 / 空壳鞋 | 人台脚部只有轮廓，没有鞋型先验 | 按主流程第 3 步 `inpaint.py --box ... --zoom 2.2` 修，或先换鞋型再重绘 |
| 取到的图 404 | 该姿态没有这个机位 | `search` 输出的 `[front,high,low,side,diagonal]` 就是可用机位 |

## 性能

- 检索/取图：本地 0.14 s，线上约 0.8 s/张
- 单张生成（768×1024，8 步）：20–27 s
- 一批 9 姿态：约 4 分钟，其中布局失败需重跑 2–3 张

## 版权边界

前端代码 MIT；**图片、原始数据与名称不属于 MIT**，原作者仅允许作为创作参考（含商用创作），
禁止再分发、转载或转售图片数据本身。批量产出物交付给第三方前要自行确认合规。
