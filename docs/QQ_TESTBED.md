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
python tools/make_bundle.py --with-snowluma <snowluma目录>  # 打跨机测试包
```

`down` 按命令行特征（`node … index.mjs`）精确结束，**不会**碰用户的 QQ
进程，也不会按进程名批量杀。

---

## 7. 跨机测试（本机 + 虚拟机）

单机两个号只证明了「同一台机器上能通」。真实场景里两端在不同机器上，
中间多了网卡、NAT、防火墙，所以**必须另测一遍**。

### 7.1 打一个能拷进虚拟机的包

```bash
python tools/make_bundle.py --with-snowluma D:/Projects/SnowLuma-v1.14.22-win-x64-lite
```

产物 `dist/qqtest-<版本>-<时间>.zip`（约 75 MB），解压后：

| 文件 | 用途 |
|---|---|
| `wheels/` | 本库 + 依赖的 wheel，`--no-index` 离线装 |
| `snowluma/` | 运行所需的 SnowLuma 文件（含空的 `config/`） |
| `install.ps1` | guest 端装依赖并自检 |
| `run-snowluma.ps1` | 起 SnowLuma，等 OneBot 端口，识别 EULA 卡住 |
| `probe.ps1` | 发一张图，验 OneBot 通不通 |
| `CHECKLIST.md` | 手工步骤、端口表、排错表 |
| `manifest.json` | 版本、md5、端口分配 |

打包行为：

- 依赖按 **cp311–cp315 × win_amd64/win32 全部下一遍**（10 个组合）。装机时
  pip 自己挑。三种情况都实际踩过：
  - 只打 cp311，装到 3.12 上 → `No matching distribution found for numpy`
  - 只打 `win_amd64`，装到 32 位 Python 上 → 同样报错（32 位的 wheel
    名字是 `win32`，很多人在 64 位 Windows 上装了 32 位 Python）
  - Python 3.14/3.15 上 `uvicorn[standard]` 的 C 扩展还没编好，会
    `ResolutionImpossible` 带崩整条依赖链 → 自动退回纯 Python 的 uvicorn
- `install.ps1` 装之前先打印 Python 版本/位数，版本低于 3.11 直接说清楚；
  装失败时列出包里实际有哪些 `numpy-*.whl`，不用人猜。
- 装的是 `markovcryptor[web]` 而不是 `markovcryptor`，否则 guest 上没有
  `fastapi` / `uvicorn`。
- 包内**不含 Python 与 Node 本体** —— 这两样 guest 要自己装。

### 7.2 Win10 上的三个编码/策略坑

Win10 自带 PowerShell 5.1，`ps1` 本身不是问题，但有三处会挡路。

**（一）执行策略 `Restricted`**

Win10 默认就是 `Restricted`，双击或直接跑 `.ps1` 会报：

```
无法加载文件 …install.ps1，因为在此系统上禁止运行脚本。
FullyQualifiedErrorId : UnauthorizedAccess
```

包里每个 `.ps1` 都配了一个 `.bat`，双击即可 —— `.bat` 里带
`-ExecutionPolicy Bypass`，只影响那一次进程，不改系统设置、不需要管理员。

也可以手工：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\install.ps1
```

**（二）`.ps1` 必须带 UTF-8 BOM**

PS 5.1 读无 BOM 的 `.ps1` 时按系统 ANSI（中文机是 GBK）解码。脚本里的
中文注释会被解错，`号` 的尾字节 `` 与行尾 `
` 结合成一个汉字，
**吃掉 CRLF**，把 `param(` 并进上一行注释，于是报：

```
-File 格式不支持实际参数 .\probe.ps1
```

这不是「参数没给」，是整段 `param()` 消失了。所以生成 `.ps1` 时
**BOM + CRLF 都是硬要求**，有测试盯着。

**（三）控制台代码页 936**

即使脚本解码对了，输出中文还是乱。`.bat` 里 `chcp 65001`，`.ps1` 里
`[Console]::OutputEncoding = UTF8`，Python 子进程里
`sys.stdout.reconfigure(encoding="utf-8")` —— 三层各管一段。

`.bat` 本身只能放 ASCII（cmd 走 OEM 代码页），所以 `.bat` 里的提示是英文。

### 7.3 两个容易卡住的地方

**（一）首次启动会停在 EULA**

干净的 SnowLuma 没有 `config/consent.json` 时日志停在
`awaiting EULA/PRIVACY consent`，此时 **QQ hook 和 OneBot 端口都不会起**。
需要先启一次，从日志里拿随机初始密码（`initial credentials: user=admin
password=…`），登录 `http://127.0.0.1:5099/` 同意，之后就不问了。
`run-snowluma.ps1` 会识别这个状态并直接说明，不会让人去查网络。

**（二）跨机要改监听地址**

`onebot_<UIN>.json` 的 `wsServers[0].host` 默认 `127.0.0.1`，只能本机连。
要跨机：

```bash
python -m markovcryptor.cli qq-init --snowluma <dir> --uin <UIN> \
  --ws-port 9721 --http-port 9722 --host 0.0.0.0 --force
```

虚拟机网络用**桥接**最省事；用 NAT 的话要在虚拟网络编辑器里加端口转发。
只在本机测时**不要**用 `0.0.0.0`。

### 7.4 跨机之后必须重跑 P0

单机测得的「图片逐字节原样往返」是在同一条回环路径上得到的。跨机之后
中间多了虚拟机网络，所以第 5 节的三层验证要按顺序重做，特别是第 3 层：

```bash
python tools/qq_probe.py \
  --a-uin A --a-ws ws://127.0.0.1:9711/  --a-token mkx-A \
  --b-uin B --b-ws ws://<guest IP>:9721/ --b-token mkx-B
```

如果这里变成「被改动」，说明中间有环节重编码了图片，像素域隐写不可用，
得退回中文信道。

### 7.5 websockets 版本

`websockets` 14 起删掉了 `legacy` 实现，鉴权头参数从 `extra_headers`
改名为 `additional_headers`。项目里所有连 OneBot 的地方统一走
`markovcryptor.onebot_ws.connect()`，它会按装着的版本挑参数名。
有测试（`OneBotWebSocketTest`）盯着，禁止再手写 `extra_headers=`。

---

## 8. 打通双方互联：把终端挂到真 QQ 上

前面几节验证的是「QQ 会不会破坏图片」，结论是**不会**。这一节是把它真正
用起来：两个终端各自挂一个 QQ 号，握手和密文都走 QQ。

### 8.1 一条命令

```bash
python -m markovcryptor.cli serve --port 8731 \
  --onebot "3925697820=ws://127.0.0.1:9711/;mkx-3925697820" \
  --onebot "1741473279=ws://127.0.0.1:9721/;mkx-1741473279"
```

不给 `--onebot` 就是原来的**本地双窗演练**（一条内存队列，一定送达）。
给了就换成真的 QQ，而 `Terminal` / `Session` / 协议层**一行没改** —— 这正是
[QQ_TRANSPORT.md](QQ_TRANSPORT.md) §3 那个分层当初留口子的目的。

`--psk` 是**隐写层**的共享口令，两侧必须一致。不一致不会报错，只会表现为
「发出去的图看起来是普通照片」——因为帧根本没通过认证。默认 `pbx1`。

打开 `http://127.0.0.1:8731/` 就是同一套界面，`/terminal/state` 里的
`transport` 会变成 `onebot`。

### 8.2 不想开浏览器：命令行直连

`tools/qq_chat.py` 是一个两端的控制台，适合压链路：

```bash
python tools/qq_chat.py --a-uin 3925697820 --a-port 9711 \
                        --b-uin 1741473279 --b-port 9721
```

```
[alice] handshake            # 发起握手，公钥藏进图里
[alice] send 今晚八点老地方    # 密文藏进图，明文不上网
[bob]   ...（自动回包后会打印会话已建立）
/as bob                      # 切到另一侧
```

跨机时每台各跑一侧：

```bash
python tools/qq_chat.py --single a --a-uin <本机号> --a-port 9711 \
                        --peer <对端号> --a-token mkx-<本机号>
```

不接 QQ 也能验整条链路：

```bash
python tools/qq_chat.py --selftest
```

它会用内存里的假 OneBot 跑完整往返（握手 → 密文 → 反向密文），退出码即结论。

### 8.3 传输层要解决的三件事

都写在 `terminal/onebot.py` 里，因为它们在 QQ 上是真实存在的，不是理论问题。

**（一）一条消息一张图，没有分组**

P0 实测（[QQ_TRANSPORT.md](QQ_TRANSPORT.md) §4 第 2 条）发现多张图**可能**被
合并推送、也**可能**被拆开。所以一个带 n 张图的包会发成 n 条独立消息，每张图
带一个小尾巴说明「我是哪个包的第几张」：

```
magic(4) | packet_id(8) | index(1) | count(1)
```

这个尾巴**贴在 PNG 的 IEND 之后**，不在 PBX1 信封里。两个理由：

* 信封是认证过的密文，把路由信息塞进去等于让传输层依赖密码学层；
* IEND 之后的字节所有解码器都会忽略，所以图片的像素**一个都没变**，
  藏在像素里的隐写载荷自然也不受影响。

收侧按 `packet_id` 攒齐再交付；攒不齐的按超时丢弃并记日志，绝不拿半截去解密。

**（二）没有尾巴的图是正常的**

对端发来的普通照片、别人随手发的图，都没有尾巴。这类图自成一路（单图包），
**永远不会**被拼进我们正在等的那个包里 —— 否则一张摄影图就能顶替一个缺失分片，
把密文拼成错误的内容。

**（三）URL 会过期**

QQ 的图片链接有有效期，所以收到事件立刻下载，不缓存 URL。下载失败只计一次
`dropped` 并继续读下一条，不让一张坏图卡死整个接收循环。

### 8.4 两个容易踩的坑

**自动握手回包需要「自己的图」**

`Terminal._auto_respond` 只用本端自己的图回包（理由见那里的注释）。本地演练时
图来自浏览器队列；接 QQ 时没有浏览器，所以要服务端兜底生成一张。
`web.py` 的 `_onebot_hub` 就是这么做的。**没有兜底**的话，对端发来的握手会一直
压在 `pending_request` 里，界面上什么都不显示 —— 表现为「对方说发了，我这边没反应」。

**`attach()` 已经注册过网络了**

`Terminal.attach()` 内部就调了 `network.register()`。外面再手动
`network.register(uin, terminal.deliver)` 会让每个包投递两次：握手回两遍、
同一条密文解出两条。这个坑实际踩到了，现在有测试盯着。

### 8.5 相关文件

| 文件 | 作用 |
| --- | --- |
| `terminal/onebot.py` | `Network` 实现、包关联头、重组、`InProcessOneBot`（无 QQ 的替身） |
| `tools/qq_chat.py` | 两端控制台 / 跨机单侧 / `--selftest` |
| `tests/test_onebot_transport.py` | 28 项：尾巴、乱序重组、普通图不越界、真双端握手与密文 |
| `web.py` `_onebot_hub` | 把 hub 挂到 OneBot，并准备兜底底图 |
