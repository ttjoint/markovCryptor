# 架构方案：从可行性原型到多信道隐写平台

> 状态：**提案（v1）**。本文只描述目标架构与迁移路径，不包含已完成的实现。
> 撰写依据：现有 19 个 Python 模块（4 764 行）、64 项测试、全库 15 488 张图的压测数据。

---

## 1. 现状评估

### 1.1 已经做对的部分

先说什么**不需要推翻**。这套代码的分层比它的目录结构看起来健康得多：

```
arith ────────────────┐
progress ─────────────┤
punctuation ──┐       │
compression ──┴─ model ┴─ codec ── cli
image_stego ──┬─ gif_stego ── chat_gui
              └─ handshake
```

（由 `ast` 实测的模块内依赖，**无环**。）

- **三个信道内核彼此独立**，且都不依赖任何 UI：`image_stego` 零内部依赖，
  `gif_stego` 只依赖 `image_stego`，`codec` 只依赖算法层。
- **`progress.py` 的进度/取消抽象是现成的好东西**：`ProgressFn(fraction, message)`
  + `Cancelled` 异常，被 8 个模块 import。这正是长任务（训练、大图嵌入、
  视频转码）在 Web 后端需要的形态，不用重造。
- **算法内核与 IO 已经分离**：`image_stego.embed` 收 `Image` 返 `Image`，
  `codec.encode` 收 `bytes` 返 `str`。没有一个内核函数知道文件路径或 HTTP。
- **测试是可用的行为基线**：64 项测试覆盖了三信道的往返、容量判定一致性、
  鲁棒性、错误拒绝。重构时可以拿它当安全网。

### 1.2 阻碍平台化的具体问题

按严重程度排序，每条都指向具体的代码位置：

#### P0-1：没有任何「信道」抽象，三个信道语义相同但接口不同

这是最核心的问题。三者的**语义完全一致**——「用 key 把 payload 藏进载体，
接收方盲提取还原」——但签名毫无共性：

| 信道 | 载体类型 | 载荷容器 | 嵌入返回 | 关键参数 |
|---|---|---|---|---|
| `codec.MarkovStegoCodec` | `str`（中文文本） | 类实例，需先持有 model | `(str, CodecStats)` | `password`、`punctuate`、`rng` |
| `image_stego` | `PIL.Image` | **模块级函数** | `(Image, dict)` | `step`、`key: bytes` |
| `gif_stego` | `PIL.Image` / `bytes` | **模块级函数** | `(bytes, dict)` | `frames_to_use`、`step` |

后果：每加一个信道（视频、音频、直播流），上层就要再写一遍
「选信道 → 检查容量 → 编码 → 报告」的分支。`chat_gui.py`（494 行）就是这样
被写出来第二遍的——它把图片信道的 `check_cover`、`upscale_cover`、`plan`、
`try_extract` 全部直调了一遍。

**而且 `__init__.py` 至今没导出 `image_stego`/`gif_stego`/`handshake`**——
图片信道是作为外挂挂在包外的，包级 API 还停留在纯中文信道时代。

#### P0-2：没有容量/可行性探测的统一入口

每个信道的「能不能装下 N 字节」判定分散在四处：

- `image_stego.check_cover(image, payload_bytes) -> (bool, str)`
- `gif_stego.capacity(image) -> dict`
- `codec` 完全没有容量概念（中文信道容量取决于模型熵 × 文本长度）
- `chat_gui.CoverInfo` 又包了一层

上层想问「这张图/这个 GIF/这段文本能装多少」时，得知道用哪个信道的哪个函数。
批量处理（P0-3）和 Web API 都迫切需要这个统一入口。

#### P0-3：批处理 = for 循环，没有作业模型

`tools/bench.py`（353 行）已经把「遍历目录 → 逐文件 → 收集结果 → 汇总」
写了一遍，但它是**基准测试工具**，不是批处理设施：没有断点续跑、
没有并发控制、失败不重试、结果只在内存里。真正的批处理需要作业队列、
进度持久化、部分失败的处理策略。

#### P0-4：无服务化边界

- 全局可变状态：`image_stego` 用模块级常量，模型通过参数逐个传递，
  没有「引擎实例」的概念。Web 服务需要可注入、可缓存的单例。
- 模型加载 0.15 s/个、6 MB/个，必须进程内缓存——现在每次 `cli` 调用都重载。
- 无并发考量：`numpy` 大数组运算会释放 GIL，但 Pillow 解码不会，
  需要线程池 + 任务队列隔离。
- 无请求追踪、无配额、无大文件流式处理。

#### P1-1：GUI 与业务逻辑耦合

- `gui.py` 840 行：`MarkovCryptorApp(tk.Tk)` 一个类里同时有模型列表、
  训练对话框、编解码表单、进度条、文件选择。逻辑无法被 CLI 或 Web 复用。
- `chat_gui.py` 494 行：直调 `image_stego` 底层，包含 `CoverInfo` 这类
  本应属于领域层的逻辑。
- 两个 GUI 各自有 `main()` 和独立的 `run_*.py` 入口，没有任何共享的
  「应用服务」层。

#### P1-2：包结构与命名不再准确

- 包名 `markovcryptor` 绑定在「马尔可夫链中文隐写」上，但项目已含图片、
  GIF 信道，目标是通用平台。
- 顶层 19 个模块平铺，算法层/领域层/接口层混在一起
  （`arith.py` 与 `gui.py` 同级）。
- 没有 `py.typed`、没有 `pyproject.toml` 的正式项目元数据
  （现在只有一个 pytest 配置）。

#### P2-1：错误类型不成体系

`StegoError`、`GifStegoError`、`HandshakeError`、`ValueError`、`RuntimeError`
混用。Web API 需要稳定映射到 HTTP 状态码：容量不足 → 413、
密钥错误 → 401、载体损坏 → 422、内部错误 → 500。

#### P2-2：无可观测性

没有结构化日志、没有指标（耗时、成功率、容量利用率）。压测数据是靠
`tools/bench.py` 临时统计的，线上无同类数据。

---

## 2. 目标架构

### 2.1 设计原则

1. **信道是一等公民（Channel as a plugin）**。每个信道实现同一个协议，
   上层（CLI/Web/GUI/批处理）只面对协议。
2. **内核不碰 IO**。加解密、隐写、容量计算都是纯函数式的；
   文件、网络、数据库在边缘层。
3. **一个引擎实例，三种界面**。CLI / Web / GUI 共用同一套应用服务层，
   界面只负责「收集输入、展示输出、转发进度」。
4. **渐进迁移，不推倒重来**。现有 64 项测试是安全网；
   每一步重构都必须让它保持绿色。
5. **能力可探测（capability discovery）**。新信道注册后，界面自动出现它，
   不需要改 UI 代码。

### 2.2 分层视图

```
┌───────────────────────────────────────────────────────────────────┐
│  接口层  Interfaces                                                │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────────────┐   │
│  │   CLI    │  │  Web API │  │ Desktop  │  │ Python SDK       │   │
│  │ (Typer)  │  │(FastAPI) │  │  (Tk)    │  │  (import 即用)    │   │
│  └────┬─────┘  └────┬─────┘  └────┬─────┘  └────────┬─────────┘   │
└───────┼─────────────┼─────────────┼─────────────────┼─────────────┘
        └─────────────┴──────┬──────┴─────────────────┘
                             ▼
┌───────────────────────────────────────────────────────────────────┐
│  应用服务层  Application (Engine)                                  │
│   Engine.embed(channel, cover, payload, key, opts) -> Envelope     │
│   Engine.extract(channel, carrier, key, opts) -> Payload           │
│   Engine.probe(channel, cover, payload_bytes) -> Feasibility       │
│   ── 信道注册表 / 模型缓存 / 作业队列 / 进度总线 / 审计日志          │
└───────────────────────────────┬───────────────────────────────────┘
                                ▼
┌───────────────────────────────────────────────────────────────────┐
│  信道层  Channels (plugins)                                        │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌────────┐ ┌───────────┐  │
│  │ chinese  │ │  image   │ │   gif    │ │ video† │ │  stream†  │  │
│  │ (markov) │ │ (DCT-QIM)│ │ (DCT-QIM)│ │ (ffmpeg│ │  (实时)   │  │
│  └────┬─────┘ └────┬─────┘ └────┬─────┘ └───┬────┘ └─────┬─────┘  │
│       └────────────┴────────────┴───────────┴────────────┘        │
│                    ChannelProtocol + Registry                      │
└───────────────────────────────┬───────────────────────────────────┘
                                ▼
┌───────────────────────────────────────────────────────────────────┐
│  领域核心  Core                                                    │
│   framing (帧结构) │ crypto (AES-GCM/X25519) │ ecc (RS)            │
│   arith (算术编码) │ progress (进度/取消)     │ errors (错误体系)   │
└───────────────────────────────┬───────────────────────────────────┘
                                ▼
┌───────────────────────────────────────────────────────────────────┐
│  基础设施  Infrastructure                                          │
│   model store │ corpus │ config │ logging │ metrics │ storage      │
└───────────────────────────────────────────────────────────────────┘

† = 待实现（见 §5 扩展路线）
```

### 2.3 核心抽象：ChannelProtocol

这是整个方案的枢纽。用 `Protocol`（结构化子类型）而非继承，
因为三个现有信道的接口差异大，强制继承会逼出一堆空实现。

```python
# stegoplatform/channels/base.py
from typing import Protocol, Any, BinaryIO, runtime_checkable

@runtime_checkable
class Channel(Protocol):
    """把一个信道的全部能力收成一处。"""

    # --- 元数据（供能力探测与界面自动生成）---
    id: str                          # "image" / "gif" / "chinese" / "video"
    display_name: str                # "图片（DCT-QIM）"
    carrier_kind: CarrierKind        # TEXT | IMAGE | ANIMATION | AV_STREAM
    mime_in: tuple[str, ...]         # 接受的输入 MIME
    mime_out: tuple[str, ...]        # 产出的 MIME
    options_schema: type[BaseModel]  # Pydantic：参数与约束声明

    # --- 三种操作，语义统一 ---
    def probe(self, cover: Carrier, payload_bytes: int) -> Feasibility: ...
    def embed(self, cover: Carrier, payload: bytes, key: bytes,
              options: BaseModel, progress: ProgressFn | None = None) -> Envelope: ...
    def extract(self, carrier: Carrier, key: bytes,
                options: BaseModel, progress: ProgressFn | None = None) -> bytes: ...
```

配套的**值对象**（`dataclass`/Pydantic，全部不含 IO）：

```python
@dataclass(frozen=True)
class Carrier:
    """载体：惰性引用，不强制加载进内存。"""
    kind: CarrierKind
    load: Callable[[], Any]          # () -> str | PIL.Image | bytes
    size_hint: int | None = None
    media_type: str | None = None

@dataclass(frozen=True)
class Feasibility:
    ok: bool
    capacity_bytes: int              # 当前载体最多能装多少
    required_bytes: int
    reasons: tuple[str, ...]         # ok=False 时的真实原因（保留原文）
    hints: tuple[str, ...]           # 「放大 1.4×」「换更大动图」
    cost: dict[str, float]           # 预计耗时/体积变化/失真，供界面示警

@dataclass(frozen=True)
class Envelope:
    """嵌入结果：可选的多种输出形态 + 统一报告。"""
    primary: bytes | str             # 主输出（PNG 字节 / 伪装文本）
    alternatives: dict[str, bytes]   # {"jpeg-q95": ..., "webp": ...}
    report: EmbedReport              # step/carrier_profile/容量利用率/失真

@dataclass(frozen=True)
class EmbedReport:
    channel: str
    payload_bytes: int
    carrier_bytes: int | None        # 载体原始/产出体积
    capacity_bytes: int
    utilization: float               # payload / capacity
    options_used: dict[str, Any]     # 实际生效参数（如 step 自动升档）
    distortion: dict[str, float]     # max_block_change 等
    elapsed_ms: int
```

**接口适配的落地方式**（关键：不改内核，只在外面包一层）：

```python
class ImageChannel:                       # 适配 image_stego
    id = "image"
    carrier_kind = CarrierKind.IMAGE
    options_schema = ImageOptions         # step, upscale, allow_jpeg

    def probe(self, cover, payload_bytes):
        img = cover.load()
        ok, reason = image_stego.check_cover(img, payload_bytes)
        return Feasibility(ok=ok, capacity_bytes=image_stego.max_payload_bytes(*img.size),
                           required_bytes=payload_bytes,
                           reasons=() if ok else (reason,),
                           hints=self._hints(img, payload_bytes), cost={})

    def embed(self, cover, payload, key, options, progress=None):
        frame = image_stego.build_frame(payload)
        stego, rep = image_stego.embed(cover.load(), frame, key, step=options.step)
        return Envelope(primary=_to_png(stego), report=_report(rep, payload))
```

`ChineseChannel` 同理，把 `MarkovStegoCodec` 的 `model` 从构造参数
改为 `Engine` 持有的模型缓存注入。

### 2.4 统一帧与密钥语义

现状：图片信道自带 `MKX1` 帧（magic/profile/nonce/nblocks/length/CRC16），
中文信道有自己的一套帧头 + `PROTOCOL_VERSION=5`。两套互不兼容。

**决策：保留各自的帧，但在领域核心层提供统一的「信封」概念。**

理由：图片帧为了盲提取必须极简（44/88 块成本已压到极限），
中文帧需要携带模型识别码；强行统一会同时伤害两者的效率。
需要统一的是**语义**而非**字节格式**：

- `key` 统一为 `bytes`，由 `normalise_key()` 归一（图片信道已有，提升到 core）
- `payload` 统一为 `bytes`，文本载荷由调用方先 UTF-8 编码
- 「盲提取」统一为：只用 key + 载体，不依赖原载体
- 「口令派生密钥」统一走 `core.crypto.derive_key(password, salt, purpose)`

---

## 3. 目录结构

```
markovCryptor/                        # 仓库根
├── pyproject.toml                    # 正式元数据 + 依赖 + 入口点
├── README.md
├── docs/
│   ├── ARCHITECTURE.md               # 本文
│   ├── CHANNELS.md                   # 各信道原理与容量（合并现有 IMAGE_STEGO.md）
│   ├── API.md                        # Web API 参考
│   └── PLUGINS.md                    # 如何接入新信道
├── src/
│   └── stegoplatform/                # ← 新包名（可保留 markovcryptor 作兼容别名）
│       ├── __init__.py               # 只导出高层 API
│       ├── py.typed
│       │
│       ├── core/                     # 领域核心，零 IO
│       │   ├── framing.py            # MKX1 帧 build/parse
│       │   ├── crypto.py             # AES-GCM、X25519、密钥派生
│       │   ├── ecc.py               # Reed-Solomon 封装
│       │   ├── arith.py             # 算术编解码（现 arith.py）
│       │   ├── progress.py          # 现 progress.py
│       │   ├── keys.py             # normalise_key / derive
│       │   └── errors.py            # 错误体系（见 §4.2）
│       │
│       ├── channels/                 # 信道插件
│       │   ├── base.py              # Channel Protocol + 值对象
│       │   ├── registry.py          # 注册表 + 能力探测
│       │   ├── chinese/
│       │   │   ├── channel.py       # ChineseChannel
│       │   │   ├── codec.py         # 现 codec.py
│       │   │   ├── model.py         # 现 model.py
│       │   │   ├── compression.py
│       │   │   └── punctuation.py
│       │   ├── image/
│       │   │   ├── channel.py       # ImageChannel
│       │   │   ├── dct_qim.py       # 现 image_stego.py
│       │   │   └── profiles.py      # LeadProfile 两档规格
│       │   ├── gif/
│       │   │   ├── channel.py       # GifChannel
│       │   │   └── encoder.py       # 现 gif_stego.py
│       │   ├── video/               # 预留（§5.1）
│       │   └── stream/              # 预留（§5.2）
│       │
│       ├── engine/                   # 应用服务层
│       │   ├── engine.py            # Engine 门面
│       │   ├── models.py            # 模型缓存（进程内 LRU）
│       │   ├── jobs.py              # 批处理作业模型
│       │   └── session.py           # 握手会话（现 handshake.py）
│       │
│       ├── interfaces/               # 接口层
│       │   ├── cli/
│       │   │   ├── __main__.py
│       │   │   ├── main.py           # Typer 应用
│       │   │   └── commands/         # encode/decode/train/serve/batch
│       │   ├── web/
│       │   │   ├── app.py            # FastAPI 工厂
│       │   │   ├── routes/
│       │   │   │   ├── channels.py   # GET /channels, POST /probe
│       │   │   │   ├── encode.py     # POST /encode
│       │   │   │   ├── decode.py     # POST /decode
│       │   │   │   ├── batch.py      # POST /jobs, GET /jobs/{id}
│       │   │   │   └── models.py     # GET /models, POST /train
│       │   │   ├── schemas.py        # Pydantic 请求/响应
│       │   │   └── deps.py           # 依赖注入（Engine 单例）
│       │   └── desktop/              # Tk 界面（保留，供离线使用）
│       │       ├── app.py            # 主窗口（现 gui.py 拆薄）
│       │       ├── chat.py           # 双窗口聊天（现 chat_gui.py 拆薄）
│       │       └── widgets/          # 复用控件（进度、模型选择、载体预览）
│       │
│       └── infra/                    # 基础设施
│           ├── config.py             # 设置（pydantic-settings）
│           ├── logging.py            # 结构化日志
│           ├── metrics.py            # 计数/耗时
│           └── storage.py            # 模型/语料/临时文件路径
├── tests/
│   ├── unit/                         # 内核单元测试（现 64 项迁入）
│   ├── integration/                  # 跨层：Engine → 文件系统
│   ├── e2e/                          # Web API / CLI 端到端
│   └── fixtures/                     # 小图、小 GIF、迷你语料
├── tools/
│   ├── bench.py                      # 保留，改为调用 Engine
│   └── make_fixtures.py              # 生成测试载体
└── data/ corpus/ models/             # 不入版本库（现 .gitignore 已排除）
```

**关于包名**：改名为 `stegoplatform` 更能反映定位，但会打断所有 import。
折中方案——**分两步**：先原地重组为子包（`markovcryptor/core`、
`markovcryptor/channels`、…），保留 `markovcryptor` 顶层名；
稳定后再评估是否改名。这一步不做，避免重构期间同时承担命名迁移风险。

> 因此实际落地路径是：
> `markovcryptor/{core,channels,engine,interfaces,infra}/`，
> 本文其余部分直接用这个路径。

---

## 4. 关键设计决策

### 4.1 信道注册与能力探测

新信道接入零改 UI：

```python
# channels/registry.py
_REGISTRY: dict[str, Channel] = {}

def register(channel: Channel) -> Channel:
    _REGISTRY[channel.id] = channel
    return channel

def get(channel_id: str) -> Channel:
    if channel_id not in _REGISTRY:
        raise UnknownChannel(channel_id, available=tuple(_REGISTRY))
    return _REGISTRY[channel_id]

def all_channels() -> tuple[Channel, ...]: ...
```

`channels/__init__.py` 里显式注册三个现有信道。Web 的 `GET /channels`
直接序列化 `options_schema.model_json_schema()`，前端据此自动生成表单——
这就是「接入新算法不用改界面」的实现方式。

**能力探测协议**：

```
GET  /channels                    -> [{id, display_name, carrier_kind, mimes, options_schema}]
POST /channels/{id}/probe         -> Feasibility
```

### 4.2 错误体系

统一根 `StegoError`，按「调用方该做什么」分类，直接映射 HTTP：

```python
# core/errors.py
class StegoError(Exception):                    # -> 500
    code: str = "internal"
    http_status: int = 500

class InvalidInput(StegoError):   code="invalid_input";   http_status=422
class UnsupportedCarrier(...):    code="unsupported";     http_status=415
class CapacityExceeded(...):      code="capacity";        http_status=413
class BadKey(...):                code="bad_key";         http_status=401
class NoHiddenFrame(...):         code="no_frame";        http_status=404
class CarrierDamaged(...):        code="damaged";         http_status=422
class ChannelUnavailable(...):    code="unavailable";     http_status=503  # 缺依赖
```

**强制约束（沿用现有好实践）**：任何 `CapacityExceeded` 必须携带
`reasons`（真实原因原文）与 `hints`（可操作建议），不得笼统归为
「无法稳定承载」。现有代码已经做到这点（如「GIF 太小：155×60 只能容纳
28 字节，需要 32 字节。放大 2.0× 会让全部 47 帧的像素量变成 4.0 倍…」），
重构时**逐字保留**。

`GifStegoError`/`HandshakeError` 保留为子类以兼容现有测试，
`except StegoError` 即可统一捕获。

### 4.3 引擎与依赖注入

```python
# engine/engine.py
class Engine:
    """所有界面的唯一入口。无全局可变状态，可安全多实例。"""

    def __init__(self, config: Config, *, model_cache: ModelCache | None = None,
                 registry: ChannelRegistry | None = None):
        self._config = config
        self._models = model_cache or ModelCache(config.model_dir)
        self._channels = registry or default_registry()

    def channels(self) -> tuple[ChannelInfo, ...]: ...

    def probe(self, channel_id: str, cover: Carrier, payload_bytes: int) -> Feasibility: ...

    def embed(self, channel_id: str, cover: Carrier, payload: bytes,
              key: bytes, options: dict | None = None,
              progress: ProgressFn | None = None) -> Envelope: ...

    def extract(self, channel_id: str, carrier: Carrier, key: bytes,
                options: dict | None = None,
                progress: ProgressFn | None = None) -> bytes: ...

    def submit(self, job: BatchJob) -> JobId: ...      # 批处理
    def job_status(self, job_id: JobId) -> JobStatus: ...
```

**模型缓存**（实测 0.15 s / 6 MB 每模型，必须缓存）：

```python
class ModelCache:
    """进程内 LRU。key 为模型路径的 (mtime, size)，文件改动自动失效。"""
    def get(self, path: Path) -> MarkovModel: ...
```

Web 侧用 `lru_cache` 或 `Engine` 单例挂到 `app.state`；
CLI 每次调用重建（进程本来就短命）；GUI 持有长生命周期实例。

### 4.4 批处理作业模型

这是「执行批处理」需求的落点，也是最容易被做成 `for` 循环的地方。

```python
@dataclass(frozen=True)
class BatchJob:
    channel_id: str
    items: tuple[BatchItem, ...]     # 或由 input_glob 展开
    key: bytes
    options: dict
    on_error: Literal["abort", "skip", "retry"] = "skip"
    max_workers: int = 4
    checkpoint: Path | None = None    # 断点续跑

@dataclass(frozen=True)
class JobStatus:
    job_id: str
    state: Literal["queued","running","done","failed","cancelled"]
    total: int; succeeded: int; failed: int; skipped: int
    current: str | None
    progress: float
    results: tuple[BatchResult, ...]
    started_at: float; elapsed: float
```

实现要点（都会直接影响可用性）：

- **并发**：`ThreadPoolExecutor`。numpy 释放 GIL 的部分能真并行；
  Pillow 解码/编码不能，所以 worker 数默认 `min(4, cpu)`，
  并允许按信道覆盖（`Channel.concurrency_hint`）。
- **断点续跑**：每完成一项追加一行 JSONL。重启时读回已完成集合。
- **部分失败**：默认 `skip` 并记录原因；`abort` 遇错即停；
  `retry` 对 `CarrierDamaged` 重试 N 次。
- **进度总线**：复用 `progress.ProgressFn`，通过回调推给
  Web（SSE/WebSocket）、CLI（stderr）、GUI（队列）。
- **内存上限**：大图（已实测最大 144 Mpx）必须流式/逐项处理，
  不把整批载入内存。`Carrier.load` 惰性引用正是为此。

### 4.5 三种交互界面的职责边界

| 界面 | 技术 | 职责 | 明确不做什么 |
|---|---|---|---|
| CLI | Typer | 单次编解码、批处理、训练、`serve` 启动 | 不实现业务规则，只调 Engine |
| Web API | FastAPI | 远程调用、批处理作业、能力探测 | 不做渲染；大文件用流式上传 |
| Desktop | Tk（保留） | 离线使用、聊天演示、模型管理 | 不直调 `image_stego` 底层 |

**关键**：三者都只依赖 `Engine`。`gui.py` 840 行里属于业务的部分
（模型列表构建、训练参数校验、统计行格式化）下沉到 Engine 或
`interfaces/desktop/viewmodels.py`，Tk 类只剩控件与事件绑定。

`chat_gui.CoverInfo` 下沉为 `channels/image` 的 `CoverInfo`（领域概念），
GUI 只负责显示。

### 4.6 Web API 草案

```
GET  /health
GET  /channels                    能力探测（含参数 JSON Schema）
POST /channels/{id}/probe         容量/可行性（不实际嵌入）
POST /encode                      单次嵌入
     body: multipart (cover 文件, payload, key, options)
     resp: 二进制（按 Accept 协商：PNG/JPEG/WebP/文本）+ X-Stego-* 头
POST /decode                     单次提取
POST /jobs                        提交批处理
GET  /jobs/{id}                   状态（支持 ?wait=30 长轮询）
GET  /jobs/{id}/events            SSE 进度流
DELETE /jobs/{id}                 取消
GET  /models                      模型列表
POST /models                      训练（异步作业）
GET  /sessions/{id}/handshake     双窗口会话（可选）
```

**响应头约定**（让浏览器/脚本能读到报告，而不只是二进制）：
`X-Stego-Channel`、`X-Stego-Capacity`、`X-Stego-Utilization`、
`X-Stego-Step`、`X-Stego-Elapsed-Ms`。

**限制**：默认请求体上限可配，大图走 `X-Upload-Stream`；
`probe` 不落盘。

### 4.7 可观测性

- 结构化日志（JSON）：`channel`、`payload_bytes`、`capacity`、
  `utilization`、`elapsed_ms`、`step`、`outcome`。
- 指标（进程内计数器 + Prometheus 文本端点）：
  `stego_requests_total{channel,outcome}`、
  `stego_elapsed_seconds{channel}`（直方图）、
  `stego_capacity_utilization{channel}`。
- 这些指标正是压测里手工统计的东西，线上直接可得。

---

## 5. 扩展路线：新信道如何接入

这是平台化最重要的验收标准——**加一个信道不应改动任何界面代码**。

### 5.1 视频隐写（`channels/video`）

已具备的基础：环境有 `opencv-python 4.11`、`PyAV 13.1`。

方案：**I 帧空域/变换域 QIM，而非全帧**。

- 只嵌 I 帧（关键帧）：P/B 帧由运动补偿重建，任何改动都会被
  后续帧的预测误差吞掉或放大成可见块效应。
- 载体：I 帧的 Y 平面 8×8 DCT 中频 —— **与静态图完全相同的 QIM 内核**，
  `dct_qim.py` 可直接复用，这正是分层带来的红利。
- 容量：`I帧数 × 每帧容量`。H.264 默认 GOP 250，25 fps 下约每 10 秒 1 个 I 帧，
  所以 1 分钟视频只有 6 个关键帧 —— 容量远低于静态图，需要明确告知用户。
- 鲁棒性：必须用 `-crf 18` 以上高质量重编码并实测；预计
  H.264 二次压缩会破坏细粒度 QIM，需要比图片更粗的 step。
- 多规格：视频天然适合「更多帧 × 每帧更少块」的扩展，
  可直接复用 `LeadProfile` 的档位思想。

**待验证的风险**：不同编码器（x264/x265/硬件编码）对 I 帧的
量化差异很大，必须在 `docs/CHANNELS.md` 给出实测矩阵后再宣称支持。

### 5.2 直播数据流隐写（`channels/stream`）

这是最激进的目标，需要**先降低预期**：

- 实时流的载体每帧都在变，且编码器持续重压缩，
  盲提取的稳定性远低于离线文件。
- 可行形态：**带内低速隐蔽信道**（每帧 1~2 比特），靠 RS 纠错
  与长时间累积。适合传递握手公钥（32 B），不适合传文件。
- 实现约束：`Channel.extract` 需要从 `BinaryIO` 增量读取，
  不能假设载体是完整的 `bytes`。协议里 `Carrier` 已支持惰性 `load()`，
  但 stream 信道需要新增 `stream_extract(reader, ...)`，
  **建议作为 Channel 协议的 v2 扩展**，不在 v1 落地。

**建议顺序**：先做视频（离线、可复现、可压测），
直播流等视频信道稳定后再评估。

### 5.3 其它候选

| 信道 | 载体 | 复用程度 | 难度 |
|---|---|---|---|
| 音频 | WAV/FLAC 采样 LSB 或相位 | RS/ECC 复用 | 低（若限定无损格式） |
| PDF | 对象流/白空格 | 帧结构复用 | 低 |
| 字体/矢量图 | 控制点微扰 | 帧结构复用 | 中 |
| 二维码 | 视觉编码 | 只需帧结构 | 低 |

新信道接入清单（写进 `docs/PLUGINS.md`）：

1. 在 `channels/<id>/` 实现 `Channel` 三个方法 + `options_schema`
2. 值对象只用 `core` 里的类型，**不 import 任何 interface**
3. 在 `channels/__init__.py` 注册
4. 加 `tests/unit/channels/test_<id>.py`：往返、容量一致性、
   错误拒绝（复用现有 64 项测试的写法）
5. 在 `docs/CHANNELS.md` 补原理与容量表
6. **不改任何 interface 代码** —— 这是验收条件

---

## 6. 迁移路径

原则：**每一步结束时测试全绿、CLI 可用**。不做「大爆炸式」重写。

### 阶段 0：建立安全网（0.5 天）

- 把 64 项测试按 `tests/unit/` 重组（只挪文件，不改断言）
- 加 `tools/make_fixtures.py` 生成小图/小 GIF/迷你语料，
  摆脱对 `data/`、`corpus/` 的依赖（当前测试已基本做到）
- `pyproject.toml` 补正式元数据、依赖、入口点
- 建立 `tests/e2e/` 骨架：现在 CLI 的行为快照

**验收**：`python -m unittest discover` 64 项全绿。

### 阶段 1：抽 core 层（1 天）

无行为变化，纯搬运 + 改 import：

- `arith.py`、`progress.py` → `core/`
- 从 `image_stego.py` 抽出 `framing`（`build_frame`/`parse_frame`/`LeadProfile`）、
  `ecc`（RS 封装）、`keys`（`normalise_key`/`derive`）
- 从 `codec.py`/`handshake.py` 抽出 `crypto`
- 新建 `core/errors.py`，让现有异常继承它（保留原名）

**验收**：64 项全绿；`image_stego` 变成纯粹的 DCT-QIM 内核（约 400 行）。

### 阶段 2：Channel 抽象 + 三个适配器（2 天）

**这是最关键的一步，也是整个方案的核心价值。**

- 写 `channels/base.py`（Protocol + `Carrier`/`Feasibility`/`Envelope`/`EmbedReport`）
- 写 `channels/registry.py`
- 三个适配器：`ImageChannel`、`GifChannel`、`ChineseChannel`
- **不删任何旧函数**，适配器内部调用它们
- 新增测试：三信道经同一接口各自往返 + `probe` 与直接调用的容量判定一致

**验收**：以下代码对三个信道都能跑通

```python
engine = Engine(Config())
for ch in engine.channels():
    feas = engine.probe(ch.id, carrier, len(payload))
    if not feas.ok: continue
    env = engine.embed(ch.id, carrier, payload, key)
    assert engine.extract(ch.id, env.as_carrier(), key) == payload
```

### 阶段 3：Engine + CLI 迁移（1.5 天）

- 实现 `Engine`、`ModelCache`
- CLI 改为 Typer，所有子命令改调 `Engine`
- 保留旧 `argparse` 入口一段时间（子命令名与参数不变），
  确保现有脚本与 GUI 不炸
- 加 `tests/e2e/cli/`

**验收**：`encode`/`decode`/`train`/`models`/`identify`/`demo` 行为不变；
新增 `channels`/`probe` 子命令。

### 阶段 4：Web API（2 天）

- FastAPI 应用工厂 + `deps.py` 注入 Engine 单例
- `/channels`、`/probe`、`/encode`、`/decode`
- 错误 → HTTP 映射（`core/errors.py` 已带 `http_status`）
- `X-Stego-*` 响应头
- `tests/e2e/web/`：用 `httpx.ASGITransport`，不需真起服务

**验收**：三信道经 HTTP 往返；错误码正确；大图不 OOM。

### 阶段 5：批处理作业（1.5 天）

- `engine/jobs.py`：作业模型、线程池、JSONL checkpoint
- `POST /jobs`、`GET /jobs/{id}`、SSE 进度
- CLI `batch` 子命令
- `tools/bench.py` 改为调用 Engine（顺带获得断点续跑）

**验收**：用 `D:/Pictures/表情包` 跑 1 000 张，中断后能续跑；
失败项有明确原因；进度流正常。

### 阶段 6：GUI 拆薄（2 天）

- `gui.py` 的业务逻辑下沉为 `interfaces/desktop/viewmodels.py`
- `chat_gui.py` 的 `CoverInfo` 下沉到 `channels/image`
- 两个 GUI 改调 Engine
- 抽出复用控件

**验收**：两个 GUI 功能不变；GUI 层不再直接 import
`image_stego`/`gif_stego`（可用 import 检查测试断言）。

### 阶段 7：可观测性与文档（1 天）

- 结构化日志、指标端点
- `docs/CHANNELS.md`（合并 `IMAGE_STEGO.md` 并补中文信道）
- `docs/API.md`、`docs/PLUGINS.md`
- README 重写为平台定位

**总估**：约 11.5 个工作日。阶段 1~2 是风险最高的部分，
其余基本是装配工作。

---

## 7. 风险与对策

| 风险 | 影响 | 对策 |
|---|---|---|
| `Channel` 抽象过度设计 | 三信道差异被强行抹平，适配器里全是 `if` | 只抽象**语义**（probe/embed/extract 三件事），不抽象参数；参数走 `options_schema` 各自的 Pydantic |
| 中文信道容量无统一模型 | `probe` 对它意义不明 | 中文容量 = `f(模型熵, 目标文本长度)`；`probe` 返回「每 N 字可承载 M 字节」的区间，而非单一数字 |
| 大图/视频在 Web 侧 OOM | 服务被打挂 | 请求体上限、流式上传、`Carrier` 惰性加载、worker 池限流、按信道声明内存预估 |
| 视频信道实测不达标 | 宣称支持但不可用 | 先写实测矩阵（`docs/CHANNELS.md`），达不到就明确标注「实验性」并默认关闭 |
| 重构打断现有 GUI | 用户体验倒退 | 阶段 3 保留旧入口；阶段 6 才动 GUI，且有 e2e 兜底 |
| 包改名打断外部引用 | 依赖方 import 失败 | 本期不改名（见 §3 决策），只做内部子包重组 |
| 测试仍依赖本地大目录 | CI 不可用 | 阶段 0 的 fixtures 先落地，`data/`/`corpus/`/`models/` 全部移出测试路径 |

---

## 8. 需要你拍板的四个决策

1. **包名**：本期保持 `markovcryptor`（内部重组为子包），
   还是直接改为 `stegoplatform` 并承担一次性迁移成本？
   ——我建议前者。

2. **帧格式统一**：本文建议保留中文/图片两套帧（理由见 §2.4）。
   若你希望单一通用帧，会牺牲图片小图容量（引导头变大）。

3. **视频信道优先级**：是否在 Web API 之后立刻做视频（阶段 4.5），
   还是先让三信道平台稳定一段时间？
   ——我建议先稳定，视频信道需要独立的实测调优周期。

4. **桌面 GUI 的长期定位**：保留 Tk 作为离线入口，还是最终以
   Web UI（本地起服务 + 浏览器）取代？
   ——这影响阶段 6 是「拆薄」还是「冻结」。

---

## 附录 A：现有代码到目标结构的映射

| 现文件 | 行数 | 去向 |
|---|---|---|
| `arith.py` | 183 | `core/arith.py` 原样 |
| `progress.py` | 34 | `core/progress.py` 原样 |
| `codec.py` | 329 | `core/crypto.py` + `channels/chinese/codec.py` |
| `compression.py` | 328 | `channels/chinese/compression.py` |
| `model.py` | 472 | `channels/chinese/model.py` + `engine/models.py`（加载/缓存） |
| `punctuation.py` | 501 | `channels/chinese/punctuation.py` |
| `image_stego.py` | 869 | `core/{framing,ecc,keys}.py` + `channels/image/dct_qim.py` |
| `gif_stego.py` | 362 | `channels/gif/encoder.py` |
| `handshake.py` | 129 | `core/crypto.py` + `engine/session.py` |
| `cli.py` | 187 | `interfaces/cli/` |
| `gui.py` | 840 | `interfaces/desktop/app.py` + `viewmodels.py` |
| `chat_gui.py` | 494 | `interfaces/desktop/chat.py` + `channels/image` |
| `tools/bench.py` | 353 | `tools/bench.py`（改写为调 Engine） |
| 3 个测试文件 | 727 | `tests/unit/`（拆分） |

## 附录 B：现有好设计（重构时必须保留）

- `progress.ProgressFn` + `Cancelled`：长任务取消的正确形态
- `LeadProfile` 双档自适应：小图容量 +25% 的关键
- 「错误必须带真实原因 + 可操作建议」的文风
- `blocks_needed`/`max_payload_bytes`/`suggest_size`/`check_cover`
  基于单一真源（`channel_bits`）计算 —— 曾有 3 个 bug 源于此，务必维持
- 提取的「两阶段读」（先引导头后按需读块）
- 载体采样的评分（`SCORE_SAMPLES`）—— 大图性能的关键
- 测试原则：对随机敏感断言用「要么成功且正确，要么明确报错」
