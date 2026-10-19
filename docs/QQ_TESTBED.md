# QQ 双端测试环境

目标：**两个终端分别挂在两个 QQ 上，互相读写混淆数据。**

本篇写怎么把它搭起来、每一步为什么这么定、以及哪些环节自动化不了。

相关：[QQ_TRANSPORT.md](QQ_TRANSPORT.md)（接 OneBot 的设计与阶段）、
[TERMINAL.md](TERMINAL.md)（终端协议层）。

---

## 1. 先看清一件事：一份 SnowLuma 就够

原以为要"复制两份 SnowLuma、每侧配一个 QQ"。**实测发现不用。**

SnowLuma 是 hook **已登录的 QQ 进程**，而它一个实例会 hook 所有发现到的
QQ。日志里两个号各自出现 `login detected` + `pipe connected`：

```
[Hook] login detected: PID=21140 UIN=1741473279
[Hook] login detected: PID=30392 UIN=3925697820
[Hook] pipe connected: PID=21140
[Hook] pipe connected: PID=30392
[OneBot] session started: UIN=1741473279
[OneBot] session started: UIN=3925697820
```

所以单机双号只需要**一个 SnowLuma 进程**、**两份 OneBot 配置**：

```
┌───────────── SnowLuma（一个进程，WebUI :5099）─────────────┐
│  QQ 1741473279 ──► OneBot WS :9711 / HTTP :9712            │
│  QQ 3925697820 ──► OneBot WS :9721 / HTTP :9722            │
└────────────────────────────────────────────────────────────┘
```

"复制两份"只在**跨机器**时才需要 —— 那时候两个 QQ 分别在两台机器上登录，
各自的 SnowLuma 自然互不相干。

### 一个必须知道的坑

两份配置**端口不能相同**，否则第二号起不来：

```
[3925697820] [OneBot.WS-Server] [ws-default] server error:
             listen EADDRINUSE: address already in use 127.0.0.1:3000
[OneBot] network startup degraded: UIN=3925697820 failures=2
```

因为每个号读的是 `config/onebot_<自己UIN>.json`，文件名按 UIN 分开，
但端口是文件里写死的。`tools/qq_setup.py` 就是给每个 UIN 写不同端口。

---

## 2. 怎么搭

### 2.1 拉一份 release

```bash
python tools/fetch_snowluma.py                  # 最新版
python tools/fetch_snowluma.py --tag v1.14.22   # 钉死版本
python tools/fetch_snowluma.py list             # 有哪些版本
```

落到 `vendor/snowluma/`，整个 `vendor/` 已被 `.gitignore` 排除。

**不把 SnowLuma 打进本库**，三个理由：

1. `native/` 是平台相关的（Windows 是 hook QQ 的 DLL），换平台要换整个包；
2. 它有自己的 EULA，混在别的仓库里分发会模糊许可边界；
3. 它升级比我快 —— 探针写好那天上游已经是 v2.0.3，而手上这份是 v1.14.22。

### 2.2 写配置

```bash
python tools/qq_setup.py init --snowluma vendor/snowluma \
       --a-uin 1741473279 --b-uin 3925697820
```

写 `config/onebot_<uin>.json`。两个字段是一开始就要对的：

| 字段 | 值 | 为什么 |
|---|---|---|
| `messageFormat` | `"array"` | 消息段用数组，图片才能带结构 |
| `reportSelfMessage` | `false` | 自己发的消息不推回来 |

`reportSelfMessage` 这条尤其重要：终端只把"收到的包"当对端来的，自己发的
如果回显一遍，会变成自己跟自己握手。

### 2.3 起服务

```bash
python tools/qq_setup.py up --snowluma vendor/snowluma
python tools/qq_setup.py status --snowluma vendor/snowluma
```

`up` 会设 `SNOWLUMA_HOOK_AUTOLOAD=1`，自动 hook 所有已登录的 QQ，不用去
WebUI 里一个个点。然后等端口就绪再返回。

### 2.4 验图

```bash
python tools/qq_setup.py probe --snowluma vendor/snowluma
```

这是接 QQ 之前**唯一必须先做的一件事**，理由见下节。

---

## 3. P0：图片能不能原样往返

像素域隐写（DCT-QIM）的整个前提是：**发出去的图片逐字节到达对端**。
QQ 服务端只要重编码或降采样，载荷就全废 —— 而且失败得很安静：图能看，
字节变了，提取时 AES-GCM 认证不过，表现为"没有隐写帧"。

实测结果（SnowLuma v1.14.22 + QQ 9.9.36 + Windows）：

| 场景 | 结果 |
|---|---|
| 单图 200² | `120332B md5=83f57a…` → 取回 `120332B md5=83f57a…` **完全一致** |
| 三图同发 120²/256²/512² | 3 段，顺序不变，各自**逐字节一致** |
| 大图 1024² | **未被降采样**，`3150914B` 一致 |

**结论：QQ 原样往返 PNG，像素域隐写在 QQ 上可用。**

从源码能对上的：SnowLuma `packages/protocol/src/highway/image-upload.ts`
直接传原始字节（`bytes: image.bytes`），上传信息带 `original: 1`，路径上没有
`sharp`/`resize`/`quality` 这类重编码。实测确认了源码里的读法。

另外，收到的图片段里 `file` 字段就是原文件的 md5（`83f57a….png`），
这对调试很有用 —— 一眼能看出字节有没有被改。

> 这一条只在 v1.14.22 上验过。升级 SnowLuma 后应当重跑 `probe`。

---

## 4. 哪些自动化不了

| 环节 | 能否自动 | 说明 |
|---|---|---|
| 下载 release | ✅ | `fetch_snowluma.py` |
| 写 OneBot 配置 | ✅ | `qq_setup.py init` |
| 启动 + hook | ✅ | `SNOWLUMA_HOOK_AUTOLOAD=1` |
| WebUI 密码 | ✅ | `config/webui.json` 里是 scrypt 哈希，可预置 |
| EULA 同意 | ✅ | `config/consent.json` 里是版本哈希，可预置 |
| **QQ 登录** | ❌ | SnowLuma hook 已登录的 QQ，它自己不登录 |
| **知道 QQ 号** | ⚠️ | 配置文件名按 UIN，得先登录一次才知道 |

所以"完全无人值守一键部署"做不到，但 **"一次人工登录 + 之后全自动"** 能。

### 关于 WebUI

SnowLuma 的 WebUI（`:5099`）只有两个用途对我们是必要的：看 hook 状态、
必要时手动挂载。日常跑不需要打开 —— `SNOWLUMA_HOOK_AUTOLOAD=1` 已经代劳。

它的 HTTP API 有现成的挂载入口：

```
POST /api/processes/:pid/load      # 按 PID 挂载
POST /api/processes/:pid/unload
GET  /api/processes
GET  /api/qq-list
```

鉴权是 cookie 或 Bearer，密码在 `config/webui.json`（scrypt）。真要零人工，
可以走这条路按 PID 精确挂载，但**本阶段不需要** —— 自动发现已经够了。

---

## 5. 最终形态

两个终端各挂一个 QQ，互发混淆数据：

```
终端 A（Python，本库）                          终端 B（Python，本库）
    Terminal(alice)                                 Terminal(bob)
        │ OneBotNetwork                                  │ OneBotNetwork
        ▼ ws://127.0.0.1:9711                           ▼ ws://127.0.0.1:9721
   QQ 1741473279  ──────── QQ 网络 ────────►  QQ 3925697820
                 ◄─────── QQ 网络 ────────
```

要做的就是两件：

1. `terminal/onebot.py`：`OneBotNetwork`，实现现有的
   `Network` Protocol（`send(sender, target, packet)`），约 100–150 行。
   `Terminal`、`session.py`、现有终端测试**一行不改**。
2. 传输层包关联头（`packet_id(8B) + index + count`）—— 因为 QQ 会**合并
   同一条消息里的多张图**（实测 3 张合成 1 条消息、3 个 image 段），也可能
   把一条拆开，必须能自己重组。

细节和阶段划分见 [QQ_TRANSPORT.md](QQ_TRANSPORT.md)。

---

## 6. 命令速查

```bash
python tools/fetch_snowluma.py list                        # 看版本
python tools/fetch_snowluma.py --tag v1.14.22              # 拉
python tools/qq_setup.py init    --snowluma vendor/snowluma --a-uin A --b-uin B
python tools/qq_setup.py up      --snowluma vendor/snowluma
python tools/qq_setup.py status  --snowluma vendor/snowluma
python tools/qq_setup.py probe   --snowluma vendor/snowluma [--only single|multi|big]
python tools/qq_setup.py down                              # 只结束 SnowLuma，不动 QQ
```

`down` 按命令行特征（`node … index.mjs`）精确结束，**不会**碰用户的 QQ
进程，也不会按进程名批量杀。
