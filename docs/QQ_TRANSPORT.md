# 接入 QQ 信道（SnowLuma）——可行性评估

本文评估把 SnowLuma 当作真实 IM 传输层、用 QQ 信道跑隐匿收发的可行性与
工作拆解。结论先行，理由在后。

## 0. 结论

**可行，但接入方式是"跨进程协议对接"，不是"提取代码"；且有一个必须先实测
才能定的前提。**

| 事项 | 判断 |
| --- | --- |
| 代码能否直接搬进本项目 | **不能**。SnowLuma 是 TypeScript/Node 工程（1218 个文件、pnpm monorepo、`@snowluma/proton` 私域 protobuf 运行时），本项目是 Python。搬过来等于重写整个协议栈。 |
| 建议接入方式 | **OneBot v11 over WebSocket**。SnowLuma 现成提供，本项目只写一个 `Network` 实现。 |
| 协议层改动量 | **很小**。`Network` 只有 `send()` 一个必需方法（`register()` 可选）。 |
| 最大风险 | **QQ 服务端是否改写图片像素**。会的话，现有的像素域隐写（DCT-QIM）在 QQ 上全部失效。 |
| 建议第一件事 | 做「原图往返测试」：发一张 PNG，对端下载回来逐字节比对 md5。**这一条不过，其余都不用谈。** |

## 1. SnowLuma 是什么

一个 TypeScript 运行时，把 QQ 客户端的原生会话转换成 **OneBot v11** 动作与
事件，通过 WebSocket / HTTP / WebUI / SDK / MCP 对外暴露。

和本项目的关系是**上下层**，不是并列：

```
本项目（Python）                        SnowLuma（Node）
┌──────────────────┐                  ┌──────────────────┐
│ Terminal         │                  │ protocol (QQ)    │
│  ↓ Network       │  OneBot v11      │  ↓               │
│ OneBotNetwork  ──┼── WebSocket ────►│ onebot adapter   │
│                  │                  │  ↓               │
│ (本项目已有)     │                  │ QQ 客户端        │
└──────────────────┘                  └──────────────────┘
```

关键事实（从源码确认）：

- **图片是 `{type:"image", data:{file: <url|file_id|base64>}}` 段**。
- 上传走 highway（`packages/protocol/src/highway/image-upload.ts`），
  **SnowLuma 这一侧直传原始字节，不做重编码**，并在上传信息里带
  `original: 1`（原图标记）。
- 支持的图片格式包含 `png`、`gif`、`webp`、`jpg`。
- 有 `send_private_msg` / `send_group_msg` / `send_msg`。

## 2. 为什么不能"提取通信模块"

用户设想里有一条是"把该框架下的通信模块直接提取并放入本模块"。这条路走不通：

1. **语言壁垒**。SnowLuma 依赖 `@snowluma/proton`（自研 protobuf 运行时）和
   `@snowluma/proto-defs`（私域协议定义）。Python 侧没有等价物，提取出来是
   TypeScript 源码，只能整体跑一个 Node 进程——那就已经是在做"对接"了。
2. **它本身就是"对接"形态**。SnowLuma 存在的意义就是把 QQ 协议隔在一个进程里，
   对外只说 OneBot。绕开这个边界深入内部，等于自己维护 QQ 协议，与本项目
   （密码学 + 隐写）的边界冲突，且会带来持续的协议变更维护负担。
3. **本项目已经为这件事留好了口子**。`terminal/transport.py` 的 `Network`
   Protocol 只有 `send(sender, target, packet)`；`Terminal.attach()` 用到可选的
   `register(address, sink)`。写一个 `OneBotNetwork` 大约是**100–150 行**，
   比提取+移植小两个数量级。

## 3. 现有结构哪里需要动

先看现状分层（从上到下）：

| 层 | 文件 | 职责 | 对 QQ 接入的影响 |
| --- | --- | --- | --- |
| 交互 | `webui/index.html`, `web_terminal.py`, `cli.py` | 浏览器双窗、HTTP 路由 | **几乎不用改**，只多一个"选信道"的地方 |
| 终端协议 | `terminal/terminal.py`, `session.py`, `protocol.py` | 握手、信封、切片、收发判定 | **完全不用改**（这正是当初分层的收益） |
| 传输 | `terminal/transport.py` | `Network` Protocol + `LoopbackNetwork` | **新增一个实现** |
| 信道 | `channels/`（8 个文件） | `Channel` Protocol、image/gif/chinese | 不改 |
| 密码学/编码 | `core/`, `image_stego.py`, `gif_stego.py` | X25519、AES-GCM、RS、QIM | 不改 |

需要新增/改动的地方，按工作量排序：

### 3.1 新增 `terminal/onebot.py`（核心，~150 行）

```python
class OneBotNetwork:
    """OneBot v11 transport. Speaks WebSocket to SnowLuma."""

    def __init__(self, url, self_id, *, token=None): ...
    def register(self, address, sink): ...      # address = QQ 号
    def send(self, sender, target, packet): ...  # Packet → OneBot 消息段
    # 后台线程收事件 → 解出图片 → sink(sender, packet)
```

难点不在网络，在**把 `Packet` 映射成一段可发出去的 QQ 消息**：

- `Packet(images=[a,b,c], text=None)` → 三条 `image` 段（或三次发送）。
- 收侧：拿 `image.url` 下载 → 得到原始字节 → `_decode_packet`。
- **必须解决「哪几张图属于同一个包」**。现在的 `PKT1` 包格式把 n 张图和
  文本打包成一条消息内的多段，但 QQ 侧图片是逐张独立上传/下载的，
  到达顺序不保证，且一次发多图可能被拆成多条消息。

  → 建议：给每张图加一个**包关联头**（`packet_id(8B) + index + count`），
  收侧按 `packet_id` 攒齐再 `deliver`。这块逻辑 `terminal/protocol.py` 里
  `BODY_SLICE` 的分片思路可以直接复用，但**关联头要留在传输层**，不能塞进
  加密信封（否则等于让传输层依赖密码学层）。

### 3.2 传输层需要一个"会话/寻址"概念

现在 `sender` / `target` 是字符串 id，`LoopbackNetwork` 里就是 `"alice"`/`"bob"`。
QQ 上它们是 QQ 号，且**收发双方的 QQ 号在事件里都有**，直接映射即可。但要注意：

- 私聊 vs 群聊是两种寻址（`user_id` vs `group_id`）。建议先只做**私聊**。
- 一个人可能同时和多人通信 → `Terminal` 现在的 `peer_id` 是**单个**，
  只支持一对一。接 QQ 后如果要多对多，`Terminal` 需要按 peer 分表。
  → **本期建议仍限定一对一**，把多对多列为后续。

### 3.3 `web_terminal.py` 的 `TerminalHub` 要能换传输

现在写死 `LoopbackNetwork()`。改成注入：

```python
def __init__(self, *, network=None, ...):
    self.network = network or LoopbackNetwork()
```

大约 3 行改动，但让"本地演练"和"真实 QQ"共用同一套 UI。

### 3.4 `Packet` 的图片传法要适配 QQ 的现实约束

- **QQ 会压缩图片**（待实测确认程度）。若原图能完好往返，现有 PNG 路径可用。
- **若 QQ 必然重编码**：像素域 QIM 失效。备选是走 **QQ 文件上传**
  （`flash-transfer` 支持任意文件）把隐写载荷当文件发，但那就不再"隐匿"了。
  更现实的是退到**中文信道**（文本隐写），它本来就不依赖图片。

## 4. P0 实测结果：已通过

这一步决定后面所有工作是否成立，已在写代码之前完成。

| # | 测什么 | 结果 |
| --- | --- | --- |
| 1 | 原图能否逐字节往返 | ✅ `120332B md5=83f57a…` → 取回完全相同 |
| 2 | 多图是否保持独立性 | ✅ 3 段，顺序不变，各自逐字节一致；且**合并进同一条消息** |
| 3 | 大图是否被降采样 | ✅ 1024² 未被改动（3150914B 一致） |
| 4 | 下载 URL 的有效期 | ⚠️ 未单独测；收侧应当**立刻下载**，不要缓存 URL |

环境：SnowLuma v1.14.22 + QQ 9.9.36 + Windows。工具是 `tools/qq_probe.py`，
搭建过程见 [QQ_TESTBED.md](QQ_TESTBED.md)。

**结论：像素域隐写在 QQ 上可用。**

第 2 条附带一个重要约束：同一消息内的多张图会被**合并推送**（一条
`message`、多个 `image` 段），也可能被拆开。所以包关联头不是可选项，
是必需的。

**若第 1 条失败**（换 SnowLuma 版本或换平台后要重验），方案改为：
- 隐写载荷改走**中文信道**（纯文本，本就存在且已实现）；
- 图片仅作为"占位/触发器"，不再承载载荷；
- 或者放弃 QQ，改用 Telegram/Matrix 的**文件传输**——无重编码。

## 5. 分阶段工作拆解

### 阶段 A：验证（已完成）

1. ✅ `tools/qq_probe.py` → 第 4 节四项结论已拿到。
2. ✅ 结论：**像素域可用**。

搭建与复跑见 [QQ_TESTBED.md](QQ_TESTBED.md)。

### 阶段 B：传输层 —— ✅ 已完成

1. ✅ `terminal/onebot.py`：`OneBotNetwork`（`Network` 的 QQ 实现）
   - 连 SnowLuma 的 OneBot WS 端口（双号时每号一个，见 QQ_TESTBED.md）
   - 收 `post_type=message` 事件 → 立刻下载图片 → 攒包 → `sink(sender, packet)`
   - 发：`send_private_msg`，`image` 段传 `base64://`
2. ✅ 传输层关联头：`MKXT | packet_id(8) | index(1) | count(1)`，
   贴在 PNG 的 `IEND` 之后。**不在加密信封里**——理由见
   [QQ_TESTBED.md](QQ_TESTBED.md) §8.3。
3. ✅ `InProcessOneBot`：内存里模拟 OneBot 服务端，因此 CI 不需要 QQ、
   也不需要网络。`tests/test_onebot_transport.py` 28 项全绿。

**一个包 = 一条消息**（文本段 + n 个图片段）。QQ 消息本来就是段列表，
P0 实测就是一条 `send_private_msg` 带 3 个 `image` 段、合并成一条回来的。
分组头仍然挂，但那是**保险**不是主路径：同一条实测也说它们可能被拆开，
收侧的 `Reassembler` 两种情形都能吃下。

### 阶段 C：接线 —— ✅ 已完成

1. ✅ `TerminalHub(network=..., names=..., fallback_covers=...)` 注入。
2. ✅ CLI：`serve --onebot "UIN=WS[;TOKEN]"`（可重复两次）+ `--psk`。
3. ✅ `/terminal/state` 增加 `transport` 字段（`local` / `onebot`）。
4. ✅ `tools/qq_chat.py`：两端控制台、跨机单侧、`--selftest`。

`Terminal` / `Session` / `protocol` **一行未改**，与 §7 的判断一致。

### 阶段 D：真实场景收敛（部分完成）

1. ✅ 乱序：按 `index` 复原，有测试（`pump(reverse=True)`）。
   ⚠️ 丢包：目前只按超时丢弃半包并记日志，**没有重传**——
   是否要做取决于真机上的实际丢图率，先量再决定。
2. ⚠️ 大图/小图混合尚未在真机压过；错误信息已保留真实原因
   （逐张说明哪张装不下）。
3. ✅ 不给 `--onebot` 就是本地演练，同一套代码路径。

## 6. 风险清单

| 风险 | 影响 | 应对 |
| --- | --- | --- |
| **QQ 服务端重编码图片** | 像素域隐写全废 | P0 已实测通过（v1.14.22）；升级后须复跑 `qq_probe` |
| QQ 风控 / 封号 | 无法持续测试 | 用小号；频率限制；不自动重连轰炸 |
| SnowLuma 需要真实 QQ 客户端登入 | 环境依赖重、平台受限 | 只用它做传输，核心逻辑不依赖 |
| OneBot 事件顺序不保证 | 多图包拼装错乱 | 传输层关联头 + 攒齐再投 |
| 图片 URL 有有效期 | 收侧来不及下载 | 收到事件立刻下载落盘 |
| 一对多通信 | `Terminal.peer_id` 只支持单 peer | 本期限定一对一，多对多另开阶段 |
| 法律/合规 | 第三方协议项目本身有免责声明，QQ 用户协议亦有限制 | 仅本机研究使用，不外发 |

## 7. 不建议做的事

- ❌ **把 SnowLuma 源码移植进 Python**。成本极高，维护协议变更的负担全落在本项目。
- ❌ **把 QQ 协议直接塞进 `channels/`**。图片信道的抽象是"载体读写"，
  传输是"消息怎么到对端"，两者混在一起会让现有 44 项终端测试失去意义。
- ❌ **为 QQ 单独写一套终端协议**。`Terminal` 一行都不用改，这正是本次评估
  最重要的一条正面结论。
- ❌ **让别人手工点 QQ 来验证**。不可复现，且无法断言"md5 是否一致"。

## 8. 结构梳理：可以顺手清理的

评估过程中看到的、与 QQ 接入无关但确实该处理的：

| 项 | 说明 |
| --- | --- |
| `_alice.png` / `_bob.png`（仓库根） | 调试产物，应删或进 `.gitignore` |
| `run_chat.py` / `run_gui.py`（根） | Tk 已冻结到 `interfaces/desktop/legacy/`，这两个入口是死代码 |
| `interfaces/desktop/legacy/`（1335 行） | 冻结的 Tk GUI。已不维护，且无测试；建议删除或明确标注"仅供参考" |
| `terminal/terminal.py`（780 行） | 偏大。收发判定/切片/握手都在一个类里，可拆 `inspect.py` + `send.py`；但**不建议在接入 QQ 前动**，避免同时改结构又改传输 |

## 9. 一句话总结

**传输换 QQ 的代价很小（一个 `Network` 实现），真正的风险在 QQ 会不会破坏
图片的像素。先花半天做原图往返实测，再决定是"跑在图片上"还是"跑在文本上"。**
