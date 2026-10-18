# markovCryptor

两条并列的**中文/图片隐写信道**，外加 X25519 隐写握手的 Python 实现。

```
                      ┌─ 中文信道 ─→ 一段通顺中文（codec.py）
payload ─→ 压缩 ─→ 加密 ─┤
                      └─ 图片信道 ─→ 一张普通图片 / GIF（image_stego.py, gif_stego.py）
```

两者都在**盲提取**下工作：接收方只需要共享口令/密钥，不需要原始载体。

---

## 一、中文信道（`codec.py`）

纯 Python 标准库实现（AES 选项需要 `cryptography`）。

处理流程：**压缩 → （可选）AES-256-GCM → 帧封装 → 算术编码生成汉字 → 标点层**。

- **压缩**：自动在多种方法中取最小者。中文文本用“语料文本模型”：先用自适应二阶 PPM 学习消息本身，遇到没见过的字依次回退到语料统计的一阶、零阶字符表，最后才直接编码 Unicode 码位。二进制数据则用 LZMA / BZip2 / Deflate，都不划算时原样存储。压缩必须在 AES 之前，因为密文无法再压缩。
- **隐写**：以一阶马尔可夫链为概率分布，对数据比特运行算术*解码器*来生成汉字，使伪装文本是该链的忠实采样。接收方对汉字重新做算术*编码*即可还原比特。整个过程是 32 位有限精度、流式、线性时间。
- **帧**：3 字节随机数 + 标志位 + 长度 + 4 字节校验，整体用以模型指纹为密钥的 SHAKE-256 掩码白化。校验码同时充当隐式模型识别码，额外开销约 10 字节。
- **标点层**：数据只藏在汉字里，标点、引号、换行按语料统计另行添加。解码时丢弃所有非汉字字符，因此改标点、重新排版都不影响解码。

典型效果（神墓训练的 3000 字模型，编码另一本小说的原文）：伪装文本长度约为原文的 **1.0–1.2 倍**；100k 字的编码约 3 秒，解码约 1 秒。

---

## 二、图片信道（`image_stego.py`）

把任意字节藏进图片的 **DCT 中频系数**，盲提取不需要原图。

- **载体**：8×8 块 DCT，每块取 `(2,2)…(3,3)` 四个中频系数作 4 条车道，即 **4 bit/块**。
- **QIM**：写**中心格点** `(k+0.5)*step`，左右各留半格余量，吸收 RGB↔YCbCr 往返的 ±1 取整误差。
- **盲提取**：块顺序由 `SHAKE256(key, "blocks", w, h, step)` 派生，无需坐标表，改动均匀散布全图。
- **帧结构**：引导头 + 变长载荷。引导头单独 RS 编码且**永远只占一块**，接收方先解出 `nblocks` 与 `length`，**不用猜**，提取耗时恒定约 1 秒。
- **通道择优**：极端颜色（纯红/黄）的 Y 通道会被色域裁剪卡死，自动在 Y/Cb/Cr 间按
  `stable × (0.35 + 0.65 × spread)` 打分择优，默认优先 Y。
- **小图放大**：边长不足时按整数倍 LANCZOS 等比放大到「刚好够用」，不改变构图。

- **双规格引导头**：小图用紧凑档（22 B 码字 = 44 块），大图用宽松档（44 B = 88 块），
  按底图容量自动选。128² 的握手从 184 块降到 140 块，容量 +25%。

**容量**（约 `像素数 × 0.0072` 字节，即 0.058 bit/px）：

| 尺寸 | 容量 | 中文 | 尺寸 | 容量 | 中文 |
|---|---|---|---|---|---|
| 128² | 90 B | 30 字 | 1024² | 7.5 KB | 2 516 字 |
| 256² | 441 B | 147 字 | 2048² | 30 KB | 10 094 字 |
| 512² | 1.9 KB | 621 字 | 4096² | 106 KB | 35 328 字 |

**鲁棒性**：PNG 无损 ✅；JPEG q88+ ✅（q85 以下不保证）；亮度 ±20、对比度 1.1×、加噪 ±3 ✅；缩放/旋转/裁剪 ❌（需重新握手）。

**底图要求**：边长 ≥ 20 px（不足自动放大）；**PNG 最佳**。

**性能**（全库 15 062 张实测，含 PNG 往返验证）：

| 指标 | 静态图 | GIF（426 张） |
|---|---|---|
| 成功 | 99.8%（24 张 64×64 容量为 0，明确拒绝） | 99.3%（3 张太小） |
| 中位耗时 | 0.10 s | 0.51 s |
| p90 / p99 | 0.34 s / 0.68 s | 2.6 s / 9.8 s |
| 最慢 | 20 s（144 Mpx，主要是 PNG 编解码） | 18 s（1080p / 103 帧） |

三点优化让全库总耗时从 54.6 min 降到 38.6 min：通道评分按 64 k 像素采样、
提取只读帧头声明的块数不扫全图、写回只碰用到的 8×8 块（不再复制整张平面）。

---

## 三、GIF 动图（`gif_stego.py`）

GIF 是调色板格式，直接套用图片信道会被 256 色量化摧毁。可行路径是：
**从「已量化」的像素出发 → 用更粗的 QIM step（20 起）→ 存回 GIF → 从重新加载的文件验证**。

- **只嵌首帧**：GIF 靠帧间差异压缩，逐帧都改会让体积暴涨（实测 +3.6%~+168%）。
  只改第 0 帧时，**平均体积只增加 +1.1%（最大 +4.9%）**。
- **容量**：取决于首帧像素数。表情包类（150~320 px）约 100~1400 字节，够放 X25519 公钥。
- **小 GIF**：自动等比放大（所有帧同步放大以免抖动）。
- **逐帧模式**：`frames_to_use=(0, 1, …)` 可把容量 × 帧数，代价是文件变大。

```python
from markovcryptor import gif_stego as G
data, report = G.embed_gif(gif_image, frame_bytes, key)   # 默认嵌首帧
payload, info = G.extract_gif(data, key)                  # 盲提取
```

---

## 四、隐写握手 + 加密聊天

`handshake.py` 用图片信道交换 X25519 公钥：

1. 双方各生成临时 X25519 密钥对；
2. 把 32 字节公钥封成 MKX1 帧，嵌进一张普通图片（PNG 或 GIF 首帧）发给对方；
3. 收到后**盲提取**对方公钥；
4. 双方用 `HKDF-SHA256(salt=SHA256(排序后的两个公钥), info="mkx1-handshake-v1")`
   派生 64 字节，前 32 字节给公钥较小的一方发送用，后 32 字节给另一方；
5. 之后聊天走 **AES-256-GCM**，每条消息独立 nonce。

---

## 五、快速开始

### 命令行（中文信道）

```powershell
cd D:\Projects\GongCryptor\markovCryptor
python -m markovcryptor.cli train --corpus corpus\神墓.txt
Set-Content -NoNewline secret.txt '需要隐藏的中文或二进制内容'
python -m markovcryptor.cli encode --model models\神墓.json --input secret.txt --output cover.txt
python -m markovcryptor.cli decode --input cover.txt --output recovered.txt
fc.exe /b secret.txt recovered.txt
```

- 语料编码自动识别（UTF-8 / UTF-8 BOM / UTF-16 / GBK·GB18030 / Big5）。
- `--vocab-size`（默认 3000）：汉字词表大小。越大容量越高，伪装文本越短。
- `--smoothing`（默认 1）：Witten-Bell 回退强度。调大会让文本更随机、容量略增；调小则文本更通顺。
- 加 `--password` 启用 AES-256-GCM（PBKDF2-SHA256，31 万次迭代，每条消息独立随机盐）。

### 探查信道（不下笔）

新增两个子命令，用来在下笔之前就知道“能不能装下”以及“有哪些可用信道”：

```powershell
python -m markovcryptor.cli channels
python -m markovcryptor.cli probe --channel image --input photo.png --bytes 512
```

`probe` 不可承载时退出码为 1，并打印真实原因与可操作建议，例如
`容量不足：最多 162 字节，需要 99999 字节` + `放大到 3840×3840（24.0×）即可承载`。

### 本机 Web API

```powershell
python -m markovcryptor.cli serve                  # 默认 http://127.0.0.1:8731
python -m markovcryptor.cli serve --port 9101      # 端口可配
$env:MKX1_PORT=9101; python -m markovcryptor.cli serve   # 或用环境变量
```

供本机其它程序调用：`GET /health`、`GET /channels`、`POST /probe`、
`POST /embed`、`POST /extract`。交互式文档在 `/docs`。

- **无鉴权。** 这是单机服务，调用方是同机的另一个程序；能访问本地端口的进程
  本来就能直接读密钥，加 token 只增加故障点。因此服务**只允许监听回环地址** ——
  `--host 0.0.0.0` 会被直接拒绝启动（CLI 没有绕过开关）。
- 默认端口 **8731**，刻意避开 8000/8080 这类常被开发服务器占用的端口。
- 上传上限默认 64 MB；防止一张巨型图被解码成几十 GB 像素拖死进程。

```bash
curl -F payload_file=@secret.bin -F cover_file=@photo.png      "http://127.0.0.1:8731/embed?channel=image&kind=image&key=口令" -o hidden.png
curl -F file=@hidden.png      "http://127.0.0.1:8731/extract?channel=image&kind=image&key=口令" -o back.bin
```

批处理作业（异步，提交后轮询）：

```
POST /jobs?channel=image&input_dir=...&output_dir=...&key=...
GET  /jobs/{id}              # 状态与进度
GET  /jobs/{id}/report       # 逐项结果（failures_only=true 只看失败）
POST /jobs/{id}/cancel
```

### 批量嵌入 / 提取

```powershell
python -m markovcryptor.cli batch --channel image `
  --input D:\Pictures\表情包 --output D:\out --payload secret.bin `
  --key 口令 --resume D:\out\ck.jsonl --report D:\out\r.csv
```

- **断点续跑**：`--resume` 指定的 JSONL 每完成一项就追加一行；重跑会跳过已完成项
  （实测 40 张：首次 35.0s，续跑 1.4s）。
- **单项失败不影响整批**：默认 `--on-error skip`，失败项各自记录真实原因与建议，
  写进 `--report` 的 CSV。`abort` 遇错即停，`retry` 对可重试的损坏重试。
- **并发按信道定**：图片 4、GIF 2（GIF 要为每个候选步长重编码全部帧）、
  中文 4（算术编码是纯 Python）。
- `--action extract` 反向读取；`--limit N` 只处理前 N 个；`--no-recursive` 不递归。

### 网页界面

```powershell
python -m markovcryptor.cli serve
# 打开 http://127.0.0.1:8731/
```

三个面板：**编解码**（选信道 → 参数表单自动生成 → 上传 → 下载结果）、
**批处理**（提交服务端文件夹作业、看进度、查看失败项原因）、
**信道**（各信道参数与说明）。前端是单文件、零依赖、无构建的 `index.html`，
参数表单由 `GET /channels` 的 schema 自动生成——信道加参数，网页零改动。

### 桌面界面（已冻结）

```powershell
python run_gui.py       # 中文信道（Tk，已停止演进）
python run_chat.py      # 双窗口隐写握手演示（Tk，已停止演进）
```

桌面 UI 移入 `markovcryptor/interfaces/desktop/legacy/`，仍可用但不再加功能；
新功能都在网页界面。

训练、文本编解码和文件编解码都在后台线程运行，窗口顶部显示进度条，可随时取消。

### 双窗口隐写握手演示（图片 / GIF 信道）

```powershell
python run_chat.py
```

两个并排窗口代表 Alice 与 Bob。每个窗口可以：

- **选底图** —— 从 `data/` 的 24 张 PNG 和 `data/gif/` 的 14 个动图里挑；
- **① 发送握手图** —— 把本窗口的临时公钥嵌进所选图片/动图，交给对方窗口；
- **② 打开图片…** —— 改从磁盘读入一张隐写图，模拟真实聊天往返；
- 双方都收到后自动派生会话密钥，消息框切换为 AES-256-GCM 加密聊天。

---

## 六、模块结构

| 文件 | 作用 |
|---|---|
| `markovcryptor/codec.py` | 中文信道：马尔可夫模型 + 算术编码 |
| `markovcryptor/model.py` | 一阶马尔可夫模型训练与存取 |
| `markovcryptor/image_stego.py` | 图片信道：DCT-QIM 内核（静图） |
| `markovcryptor/gif_stego.py` | GIF 动图信道（调色板适配） |
| `markovcryptor/handshake.py` | X25519 隐写握手 + HKDF + AES-GCM |
| `markovcryptor/chat_gui.py` | 双窗口聊天演示 |
| `markovcryptor/gui.py` | 中文信道图形界面 |
| `markovcryptor/core/` | 信道共享内核（见下） |
| `markovcryptor/channels/` | 统一信道接口与三个适配器（见下） |
| `markovcryptor/engine.py` | 引擎：模型缓存 + 信道调度 |
| `markovcryptor/web.py` | 本机 Web API（FastAPI，无鉴权，仅回环） |
| `markovcryptor/jobs.py` | 批处理作业：线程池、断点续跑、逐项结果 |
| `docs/IMAGE_STEGO.md` | 图片/GIF 信道完整原理与容量文档 |
| `docs/ARCHITECTURE.md` | 平台化架构方案与迁移进度 |

`markovcryptor/channels/` 把三种算法收成同一个接口——`probe` / `embed` / `extract`
三个方法加一组元数据。新增算法（视频、直播流）只需实现这三件事并注册，CLI 与
网页表单会自动出现：

| 模块 | 内容 |
|---|---|
| `channels/base.py` | `Carrier`（惰性引用）、`Feasibility`、`Envelope`、`EmbedReport` |
| `channels/protocol.py` | `Channel` Protocol + `describe()`（供 `GET /channels`） |
| `channels/options.py` | 每信道的参数 schema，网页表单据此自动生成 |
| `channels/image.py` / `gif.py` / `chinese.py` | 三个适配器，内部仍调原有内核 |
| `channels/registry.py` | 显式注册表 |

程序里用 `Engine` 即可，它负责加载模型并把调用分派给信道：

```python
from markovcryptor.channels import Carrier, CarrierKind
from markovcryptor.engine import Engine

engine = Engine()                                  # 自动发现 models/ 下的模型
for channel in engine.channels():
    feas = engine.probe(channel.id, carrier, len(payload))
    if not feas.ok:
        print(channel.id, "->", feas.reasons)      # 真实原因，不是"无法承载"
        continue
    env = engine.embed(channel.id, carrier, payload, key)
    assert engine.extract(channel.id, env.as_carrier(), key) == payload
```

也可以不经引擎直接用信道：

```python
from markovcryptor.channels import registry

for channel in registry.register_defaults():
    feas = channel.probe(carrier, len(payload))
    if not feas.ok:
        print(channel.id, "->", feas.reasons)      # 真实原因，不是"无法承载"
        continue
    env = channel.embed(carrier, payload, key)
    assert channel.extract(env.as_carrier(), key) == payload
```

`markovcryptor/core/` 是不依赖任何信道的共享层，新增信道直接复用它：

| 模块 | 内容 |
|---|---|
| `core/errors.py` | 错误体系（`code` + `http_status`，供将来的 Web API 直接映射） |
| `core/framing.py` | MKX1 帧布局、CRC16、白化、双规格 lead、RS 分块规划 |
| `core/ecc.py` | Reed-Solomon 编解码（lead 单独成码字） |
| `core/keys.py` | 口令规范化、域分离、确定性 RNG |
| `core/crypto.py` | PBKDF2+AES-GCM、X25519、HKDF 会话链 |
| `core/arith.py` | 算术编码器/解码器与比特流 |
| `core/progress.py` | 进度回调与 `Cancelled` |

---

## 七、测试

```powershell
python -m unittest discover -s tests
```

共 166 项：中文信道 25 项、图片信道 26 项、GIF 信道 13 项、统一信道接口 20 项、
引擎与缓存 14 项、批处理作业 20 项、CLI 端到端 16 项、Web API 24 项、Web UI 8 项。
全部夹具随仓库分发，**不依赖 `data/`、`corpus/`、`models/`**，干净检出即可跑完，
无跳过。若需重新生成夹具（例如某个测试要换形状）：

```powershell
python -m tools.make_fixtures
```

模型格式为 `markovcryptor-model-v3`，旧版模型需要重新训练。
