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
- **帧结构**：固定 20 字节引导头 + 变长载荷。引导头单独用 `RS(40,20)` 编码，接收方先解出 `nblocks` 与 `length`，**不用猜**，提取耗时恒定约 1 秒。
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

**性能**：图库 15 k 张实测，中位 0.12 s；33 Mpx 大图约 1.7 s
（通道评分按 64 k 像素采样，提取只读帧头声明的块数，不扫全图）。

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

### 图形界面（中文信道）

```powershell
python run_gui.py
```

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
| `markovcryptor/image_stego.py` | 图片信道：DCT-QIM（静图） |
| `markovcryptor/gif_stego.py` | GIF 动图信道（调色板适配） |
| `markovcryptor/handshake.py` | X25519 隐写握手 + HKDF + AES-GCM |
| `markovcryptor/chat_gui.py` | 双窗口聊天演示 |
| `markovcryptor/gui.py` | 中文信道图形界面 |
| `docs/IMAGE_STEGO.md` | 图片/GIF 信道完整原理与容量文档 |

---

## 七、测试

```powershell
python -m unittest discover -s tests
```

共 64 项：中文信道 25 项、图片信道 26 项、GIF 信道 13 项。

模型格式为 `markovcryptor-model-v3`，旧版模型需要重新训练。
