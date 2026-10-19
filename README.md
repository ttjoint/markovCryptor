# markovCryptor

**多信道隐写加解密平台**：三条信道、统一的插件接口、CLI / Web API / 网页界面 /
批处理。全部在**盲提取**下工作——接收方只需要共享口令或密钥，不需要原始载体。

```
                        ┌─ 中文信道 ─→ 一段通顺中文    (codec.py)
payload ─→ 压缩 ─→ 加密 ─┼─ 图片信道 ─→ 一张 PNG 照片    (image_stego.py)
                        └─ GIF 信道 ─→ 一张动图          (gif_stego.py)
```

## 两种用法

| 我是…… | 用这个 |
|---|---|
| 只想藏个文件 | `python -m markovcryptor.cli serve` → 打开 <http://127.0.0.1:8731/> |
| 写脚本批量处理 | 同上的 HTTP API（见 [docs/API.md](docs/API.md)） |
| 不想离开终端 | `python -m markovcryptor.cli encode / decode`（见下方「快速开始」） |
| 要给项目加一条新信道 | 实现三个方法，见 [docs/PLUGINS.md](docs/PLUGINS.md) |
| 想要点到点安全聊天 | `python -m tools.terminal_demo`（见 [docs/TERMINAL.md](docs/TERMINAL.md)） |

## 文档

| 文档 | 内容 |
|---|---|
| [docs/CHANNELS.md](docs/CHANNELS.md) | 三条信道的原理、容量、要求、错误分类 |
| [docs/IMAGE_STEGO.md](docs/IMAGE_STEGO.md) | 图片/GIF 信道完整原理（DCT-QIM、帧结构、压测数据） |
| [docs/API.md](docs/API.md) | Web API 端点、错误码、安全边界、可观测性 |
| [docs/PLUGINS.md](docs/PLUGINS.md) | 如何新增一条信道（附可运行的完整示例） |
| [docs/TERMINAL.md](docs/TERMINAL.md) | 点到点通信终端：消息模型、握手状态机、隐写判定规则 |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | 平台化架构与迁移进度 |

---

## 一、三条信道一览

原理、容量推导与错误分类见 [docs/CHANNELS.md](docs/CHANNELS.md)；
图片/GIF 的 DCT-QIM 内核与压测数据见 [docs/IMAGE_STEGO.md](docs/IMAGE_STEGO.md)。

| | 中文文本 | 图片 | GIF 动图 |
|---|---|---|---|
| 载体 | **生成**（一段通顺中文） | 修改一张照片 | 修改动图首帧 |
| 盲提取 | 需要模型 | ✅ | ✅ |
| 容量 | ~1 KB | 约 `像素数 × 0.0072` 字节 | 同图片（首帧） |
| 输出 | `.txt` | `.png`（**必须无损**） | `.gif` |
| 抗二次压缩 | ❌ | ❌ | ❌ |

**没有一条信道能扛住有损二次压缩。** 微信/QQ 发图会重编码 JPEG，QIM 格点被破坏；
传输必须走**文件**方式，不要走图片预览。这是所有 DCT 域隐写的共同限制。

**中文信道**（`codec.py`）：压缩 → 可选 AES → 帧 → 用一阶马尔可夫链做算术*解码*
生成汉字 → 按语料统计补标点。接收方对汉字重新做算术*编码*还原比特，
改标点或重新排版都不影响解码。

**图片信道**（`image_stego.py`）：8×8 DCT 取中频 4 个系数作 4 条车道（4 bit/块），
写中心格点 `(k+0.5)*step` 留余量吸收 RGB↔YCbCr 往返误差；块顺序由密钥派生，
无需坐标表。引导头单独 RS 编码且永远只占一块，接收方先解出块数与长度，**不用猜**。
边长 ≥ 40 px；不足 120 px 时按整数倍 LANCZOS 等比放大到刚好够用。

**GIF 信道**（`gif_stego.py`）：GIF 是调色板格式，直接套图片信道会被 256 色量化摧毁。
做法是从**已量化**的像素出发、用更粗的 QIM step（20 起）、存回 GIF 后从重新加载的
文件验证。默认只嵌首帧——逐帧改会让体积暴涨，只改首帧平均只 +1.1%。

---

## 二、隐写握手 + 加密聊天

`handshake.py` 用图片信道交换 X25519 公钥：

1. 双方各生成临时 X25519 密钥对；
2. 把 32 字节公钥封成 MKX1 帧，嵌进一张普通图片（PNG 或 GIF 首帧）发给对方；
3. 收到后**盲提取**对方公钥；
4. 双方用 `HKDF-SHA256(salt=SHA256(排序后的两个公钥), info="mkx1-handshake-v1")`
   派生 64 字节，前 32 字节给公钥较小的一方发送用，后 32 字节给另一方；
5. 之后聊天走 **AES-256-GCM**，每条消息独立 nonce。

---

## 三、快速开始

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

## 四、模块结构

### 入口

| 文件 | 作用 |
|---|---|
| `markovcryptor/cli.py` | 命令行入口（`python -m markovcryptor.cli`） |
| `markovcryptor/engine.py` | 引擎：模型缓存 + 信道调度，CLI / Web / 批处理都建在它之上 |
| `markovcryptor/web.py` | 本机 Web API（FastAPI，无鉴权，仅回环） |
| `markovcryptor/webui/index.html` | 网页界面（单文件、零依赖、无构建）；终端页为左右双窗：左明文、右密文 |
| `markovcryptor/jobs.py` | 批处理作业：线程池、断点续跑、逐项结果 |
| `markovcryptor/observability.py` | 结构化日志与进程内指标（`/metrics`） |

### 信道算法内核

| 文件 | 作用 |
|---|---|
| `markovcryptor/codec.py` | 中文信道：马尔可夫模型 + 算术编码 |
| `markovcryptor/model.py` | 一阶马尔可夫模型训练与存取 |
| `markovcryptor/punctuation.py` | 标点层：按语料统计补标点 |
| `markovcryptor/compression.py` | 压缩：文本模型 / LZMA / BZip2 / 原样 |
| `markovcryptor/image_stego.py` | 图片信道：DCT-QIM 内核 |
| `markovcryptor/gif_stego.py` | GIF 动图信道（调色板适配） |
| `markovcryptor/handshake.py` | X25519 隐写握手 + HKDF + AES-GCM 会话 |

### 平台层

`markovcryptor/channels/` 把三种算法收成同一个接口——`probe` / `embed` / `extract`
三个方法加一组元数据。新增算法只需实现这三件事并注册，CLI 与网页表单会自动出现
（完整示例见 [docs/PLUGINS.md](docs/PLUGINS.md)）：

| 模块 | 内容 |
|---|---|
| `channels/base.py` | `Carrier`（惰性引用）、`Feasibility`、`Envelope`、`EmbedReport` |
| `channels/protocol.py` | `Channel` Protocol + `describe()`（供 `GET /channels`） |
| `channels/options.py` | 每信道的参数 schema，网页表单据此自动生成 |
| `channels/image.py` / `gif.py` / `chinese.py` | 三个适配器，内部仍调原有内核 |
| `channels/registry.py` | 显式注册表 |

`markovcryptor/core/` 是不依赖任何信道的共享层，新增信道直接复用它：

| 模块 | 内容 |
|---|---|
| `core/errors.py` | 错误体系（`code` + `http_status`，Web API 直接映射） |
| `core/framing.py` | MKX1 帧布局、CRC16、白化、双规格 lead、RS 分块规划 |
| `core/ecc.py` | Reed-Solomon 编解码（lead 单独成码字） |
| `core/keys.py` | 口令规范化、域分离、确定性 RNG |
| `core/crypto.py` | PBKDF2+AES-GCM、X25519、HKDF 会话链 |
| `core/arith.py` | 算术编码器/解码器与比特流 |
| `core/progress.py` | 进度回调与 `Cancelled` |

`markovcryptor/interfaces/desktop/legacy/` 是**已冻结**的 Tk 界面，
仍可用但不再加功能；新功能都在网页界面。

### 统一用 `Engine`

```python
from markovcryptor.channels import Carrier
from markovcryptor.engine import Engine

engine = Engine()                                  # 自动发现 models/ 下的模型
carrier = Carrier.of_bytes(open("photo.png", "rb").read(),
                           kind="image", media_type="image/png")
for info in engine.describe_channels():
    feas = engine.probe(info["id"], carrier, len(payload))
    if not feas.ok:
        print(info["id"], "->", feas.reasons)      # 真实原因，不是"无法承载"
        continue
    env = engine.embed(info["id"], carrier, payload, key)
    assert engine.extract(info["id"], env.as_carrier(), key) == payload
```

不经引擎、直接用某条信道也可以：

```python
from markovcryptor.channels import registry

channel = registry.get("image")
feas = channel.probe(carrier, len(payload))
if feas.ok:
    env = channel.embed(carrier, payload, key)
    assert channel.extract(env.as_carrier(), key) == payload
```

---

## 五、测试

```powershell
python -m unittest discover -s tests
```

共 252 项：中文信道 25 项、图片信道 26 项、GIF 信道 13 项、统一信道接口 22 项、
引擎与缓存 14 项、批处理作业 21 项、CLI 端到端 16 项、Web API 24 项、Web UI 17 项、
终端协议 42 项、可观测性 20 项、插件契约 12 项。
全部夹具随仓库分发，**不依赖 `data/`、`corpus/`、`models/`**，干净检出即可跑完，
无跳过。若需重新生成夹具（例如某个测试要换形状）：

```powershell
python -m tools.make_fixtures
```

模型格式为 `markovcryptor-model-v3`，旧版模型需要重新训练。
