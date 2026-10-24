# 客户端开发进度（跨对话续接用）

> **这份文件的目的**：新对话接手时先读这一页，就能知道做到哪、下一步做什么、
> 哪些结论是实测的、哪些是猜的。
>
> 设计决策见 `CLIENT.md`（为什么）。施工顺序见 `CLIENT_PLAN.md`（怎么做）。
> 本文只记录**实际做到哪一步**，与代码同步维护。
>
> 最后更新：阶段 5（零安装打包）完成，阶段 6（文本模型）开始前。

## 1. 一句话现状

客户端全部五层（传输/会话/队列/界面/启动器）完成，并产出**零安装交付包**
（dist/client_build9/qqclient-*，140MB，无需系统装 Python/Node）。
真机端到端验过：双击 start.bat -> 自动发现 QQ -> 29 好友 -> 界面正常。
**下一步是阶段 6 文本模型、阶段 7 双机验证。**

## 2. 两条产品线，别搞混

| | 测试端 | 正式客户端 |
| --- | --- | --- |
| 代码 | `markovcryptor/web_terminal.py` + `markovcryptor/webui/` | `markovcryptor/client/` |
| 定位 | alice/bob 双窗口演示 | 挂在 QQ 上的实际通信终端 |
| 好友 | 无 | 好友列表，每好友一条独立信道 |
| 状态 | **保留不动**，是唯一真机验证过的对照组 | 开发中 |

**不要动测试端。** 它跑得通，真出问题时是唯一能对照的东西。

## 3. 阶段进度

| 阶段 | 内容 | 状态 |
| --- | --- | --- |
| 1 | 传输层地基（按发送者路由 / 请求响应 / 原始事件） | ✅ 完成，真机验过 |
| 2 | 消息解析 / 好友目录 / 多会话 | ✅ 完成 |
| 3 | 图片队列（文件夹取样、预筛、上限） | ✅ 完成 |
| 4 | 新前端（好友列表 + 双窗） | ✅ 真机验过 |
| 5 | 启动器（零安装打包） | ✅ 真机验过 |
| 6 | 文本模型（懒加载、GUI 管理） | ⬜ **下一步** |
| 7 | 跨设备真机验证 | ⬜ |
| — | 交付包更新到含客户端 | ⬜ |

### 阶段 1：传输层地基 ✅

改动都在 `markovcryptor/terminal/onebot.py`：

- `register(address, sink, peer=...)`：按发送者精确投递。不传 `peer` 退回广播，
  测试端的单终端用法不受影响。**这是多好友不串台的根本。**
- `ask(action, params)`：阻塞式请求-响应，用唯一 `echo` 分辨回包。
  读循环里先 `_resolve()` 再当事件处理，否则回包会被投进会话当消息。
- `on_event(callback)`：原始消息回调，在隐写判定**之前**触发。
  隐写层对「普通图没有密文」刻意静默，界面只监听隐写层就会漏掉全部普通消息。
- `friend_list()` / `login_info()` / `friend_history()` 三个便捷方法。

**顺序依赖**：`_handle_event` 里 `on_event` 必须排在 `_sinks_for` 之前。
会话层靠这个顺序才能在陌生人第一条消息时先把会话建出来。

真机验证（真实 SnowLuma）：`login_info` → `aid 1741473279`、`friend_list` → 29 个好友、
`friend_history` → 正常返回。
**未验证**：`on_event` 真机收消息（对端离线，且 `reportSelfMessage: false` 不回推自发消息）。

### 阶段 2：解析 / 目录 / 会话 ✅

`markovcryptor/client/messages.py`
- 只正经解析图片和文本（信道只走这两样）
- 其余一行小字「对方发送了 语音」，**不猜内容**
- `reply`/`at`/`source`/`node` 静默跳过（紧挨正文，报了就是噪音）
- 连续同类只报一次；未知类型报出原名，不变成空白

`markovcryptor/client/directory.py`
- 备注优先于昵称（用户认得的是备注）
- `refresh` 整表替换而非合并（删除的好友不能变鬼影）
- 按名字排序，号码做次级键

`markovcryptor/client/conversations.py`
- 懒创建：30 个好友不预建 30 个 Terminal
- 每会话终端用 `peer=<uin>` 注册 → 不串台
- 未读只在会话**关闭**时累加
- 排序用**全局单调序号**，不是时间戳（同毫秒到达会相等，退回按名排 = 错的人跳顶）

### 阶段 3：图片队列 ✅

`markovcryptor/client/queue.py`

- 用户指定文件夹，**不用程序生成的图**（风控）
- **绝不删用户文件**，出队只是离开我们的队列
- **入队时预筛**：装不下 38 字节握手的图直接跳过并记日志
- 只先列文件名，要入队那张才读字节（几千张表情包不能全读进内存）
- 上限默认 8（可配）；`random` / `sequential` 两种取图模式
- 队列**不落盘**，退出即清空

实测容量参考：`max_payload_bytes` 在 64×64 为 **0**，120×120 为 74，200×200 为 258，
256×256 为 441，300×300 为 612。**小于 120×120 的图装不下握手。**

### 阶段 2+3 的装配层 ✅

`markovcryptor/client/account.py`
- 一个账号一个图片队列（用户点的是一「相册」，不是「给某人的图」）
- 握手回复从队列取图；队列空时**压后等待**，`retry_handshakes()` 补发
- **绝不凭空生成底图**（风控红线）
- `load_history(peer)` 点开好友才拉历史，启动时不预取
- `close()` 清空全部内存态
- `start(wait=20)` 会**先等 OneBot 连上**再拉好友：`OneBotNetwork.start()`
  只是起线程，立刻调 `login_info()` 会撞上「还没连上」——看着像配置错了，
  其实是竞态

### 阶段 4：新前端 ✅

`markovcryptor/client/app.py` —— FastAPI，端口 **8741**（避开 serve 的 8731）
- `GET /client/state`：账号 + 好友 + 会话 + 队列，一轮拉齐
- `GET /client/conversation/{peer}?history=20`：点开才拉历史；服务端只合并
  一次（`_history_merged` 标记），重复刷新不会重复灌历史
- `POST /client/send`：`mode=text/covert/handshake`。**mode 校验必须在 try 外**，
  否则 `HTTPException` 被兑底 except 抓走，422 变 502
- 用户错误（队列空/未握手）→ **409** 带可读文案；只有真故障才 502
- 图片以 data URL 内联：客户端不落盘，没有别的 URL 可给

`markovcryptor/client/webui/index.html` —— 单文件、零依赖、无构建
- 左侧好友列表（最近/好友两页签）、未读红点、信道状态标记（●已建立/○未建立）
- 右侧明文窗 + 密文窗双窗格，我方/对方气泡区分
- 降级消息（语音/抖动/卡片）显示一行斜体小字
- 短轮询 2.5s 刷新（单机单用户，不需要 WebSocket）
- `[hidden] { display: none !important; }` 一开始就写死（测试端踩过的坑）

`markovcryptor/cli.py` 新增 `client` 子命令：
```
py -m markovcryptor.cli client --onebot "UIN=ws://127.0.0.1:3001/;TOKEN" \
    --covers D:/Pictures --port 8741
```
- 启动前先等连接、拉好友；失败时一句话 + 建议，**不崩栈**
- 没给 `--covers` 时在 stderr 提醒「握手将无法发起」

**修掉的真 bug（都由测试锁住）：**

| bug | 症状 | 修复 |
| --- | --- | --- |
| `TransportError` 是裸 `RuntimeError` | `ask()` 传 `hints=` 时错误路径自己崩成 `TypeError` | 加 `__init__(message, *, hints)` |
| `Account.start()` 不等连接 | 启动必报「OneBot 还没连上」，看着像配置错 | `_wait_until_connected()` 轮询 |
| `HTTPException` 被兑底 except 吞 | mode 写错返回 502 而非 422 | 校验移出 try + `except HTTPException: raise` |

**真机验证记录（2026-10-22）：**
- SnowLuma + 真实账号 `1741473279`，29 个好友全部显示，备注优先
- 点开「三马赫」（`3925697820`）：QQ 历史自动合并进明文窗（能看到之前的
  连通性测试消息）
- 发明文 → 我方气泡正确渲染在右侧
- Playwright 截图确认三张：好友列表 / 对话打开 / 发送后
- 全量测试 **469 passed**

## 4. 测试现状

```
tests/test_client_messages.py        20 项
tests/test_client_directory.py       12 项
tests/test_client_conversations.py   18 项
tests/test_client_queue.py           16 项
tests/test_client_account.py         10 项
tests/test_client_app.py             17 项
tests/test_onebot_transport.py       PeerRoutingTest / AskTest / InboundEventTest /
                                     TransportErrorShapeTest 共 20 项
```

全量 **469 passed**（另 31 subtests）。

**注入 bug 验证过能 FAIL 的关键项**（改代码时必须保住这些）：

| 注入的错误 | 被哪项测试抓到 |
| --- | --- |
| sink 全广播，不按 peer 路由 | `test_bob_packet_does_not_enter_carol` |
| 会话不按 peer 注册 | `IsolationTest` 两项 |
| 出队时删用户文件 | `NoDeleteTest` 两项 |
| 队列空时凭空生成底图 | `test_retry_after_queue_refilled` |
| 去掉图片容量预筛 | `test_tiny_picture_is_skipped` |
| 未知消息类型静默丢弃 | `UnsupportedTest` 四项 |
| 回包不按 echo 分辨 | `test_read_loop_does_not_mistake_a_reply_for_a_message` |
| `TransportError` 不接受 hints | `TransportErrorShapeTest` 三项 |
| mode 校验塞回 try 里 | `test_unknown_mode_is_rejected`（422 变 502） |

**已知环境噪声**：`test_generated_scripts_have_right_encoding` 会真的打一次包、
下载 cp314/cp315 wheel，网络慢时卡很久（单跑约 100 秒）。不是代码问题。

## 5. 实测过的环境事实（别再去试一遍）

- 真机 SnowLuma：账号 `1741473279`（术力口宝贝），29 个好友
- 好友字段精确为 `user_id/nickname/remark`
- 历史消息 `data` 键为 `messages`，单条含 `message`（段数组）
- 图片段：`{"type":"image","data":{"url","file","sub_type","summary"}}`
- `sub_type != 0` 是表情包，通常太小装不下握手
- SnowLuma **自己生成** `config/onebot_<QQ号>.json`，文件名就是账号
  → 本机 QQ 号无需输入；对端号无法自动识别，需填一次后记在 `peer.txt`
- 配置里 `reportSelfMessage: false`（自己发的消息不回推）
- `WindowsApps\python.exe` 是微软商店 stub，**必须用绝对路径**
- `MAGIC=b"PBX1"`，握手报文共 **38 字节**
- 容量参考：64×64 → **0**，120×120 → 74，200×200 → 258，256×256 → 441，
  300×300 → 612。**小于 120×120 装不下握手**
- `TransportError(RuntimeError)`，现支持 `hints=`；与 `StegoError` 的 `hints` 约定一致
- `InvalidInput(StegoError, ValueError)`：既是 StegoError 也是 ValueError，
  所以 409 分支用 `except ValueError` 就能抓到它

## 6. 下一步（阶段 5：启动器）

目标：双击 exe 就能用，全程无控制台参数。

- Python embeddable 打包（Lib 去 site-packages 仅 21.8MB）
- launcher.exe（.NET Framework 4.8，Win10 自带）
- 探测默认浏览器，Chrome/Edge 用 `--app=URL` 开独立窗口
- 首次引导：等 QQ 登录 → 起 SnowLuma → 起客户端 → 开浏览器
- **必须用包内 python.exe 绝对路径**（WindowsApps 的 python 是商店 stub）
- 更新交付包：wheel 换成含 client 的版本，脚本加 client 启动

**遗留待定项**（到阶段 5 会撞上）：
1. 配置文件放程序目录旁还是 `%APPDATA%`（不落盘的是用户数据，配置本身要持久）
2. Firefox 无 `--app`，降级为普通标签页是否可接受
3. 文本模型无共同模型时是否自动降级图片信道（不告知对端）

## 7. 维护约定

- 改完代码**同步更新本文件的进度表和「下一步」**
- 新增注入 bug 验证过的测试，补进第 4 节的表
- 实测出来的新环境事实，补进第 5 节
- 结论与代码不一致时，**以代码为准并立刻改文档**

### 阶段 5：零安装打包 ✅

三个待定项已定（写进 `CLIENT_PLAN.md`）：
1. **模型无共同 → 静默不发**（不降级、不告知对端）
2. **配置放 `%APPDATA%\markovcryptor\client.json`**（程序目录可能只读）
3. **Firefox 降级普通窗口，可接受**

新增 `markovcryptor/client/config.py`
- 只存偏好（图片文件夹、队列上限、psk），**不含聊天数据**
- JSON 坏了 → 回默认值并记日志，不让程序起不来
- `usable_cover_folders()` 区分「没配」「配了但没图」「文件夹不存在」

新增 `markovcryptor/client/browser.py`
- 读注册表 `UrlAssociations` 拿默认浏览器
- Chrome/Edge/Brave → `--app=URL`（无地址栏独立窗口）
- Firefox → 普通标签页（已接受）
- `--app` 失败自动降级标签页，不能什么都不显示
- 本机实测：正确探测到 Chrome

新增 `markovcryptor/client/launcher.py`
- **账号来自文件名**（`onebot_<QQ号>.json`），这是「不用输 QQ 号」的全部依据
- `wait_for_endpoint()` 超时提示说「去登录 QQ」，不说「超时」
- `start_snowluma()` 自带 EULA/PRIVACY/BOOTSTRAP_PASSWORD 环境变量，
  免去首次网页点击同意
- **包自带 node 优先于 PATH**（机器上的老 node 会造成谜之失败）

新增 `markovcryptor/client/run.py` —— 无参数一键入口
- 顺序：定位 SnowLuma → 等登录 → 起 SnowLuma → 起服务 → 开浏览器
- **「配置存在」不等于「SnowLuma 在跑」**：上次退出文件留着进程没了，
  判断依据是端口是否在听。这条真机踩过：第一次启动成功、第二次必失败
- `pythonw`（无控制台）下 `_log()` 不抛
- 启动失败不写配置文件

新增 `tools/make_client_bundle.py`
- Python embeddable 3.12.6（**默认不启用 site**，要重写 `._pth` 才能用 pip）
- 离线装依赖 → 本库 → 自检（界面文件/浏览器探测/launcher 导入）
- 产出 140MB 包：python/ 37M + snowluma/ 18M + node/ 89M

**打包时踩过的坑（都值得记住）：**

| 坑 | 症状 | 根因 |
| --- | --- | --- |
| package-data 漏 client/webui | wheel 装出来界面白屏 | pyproject 只写了 `webui/*.html` |
| `WinError 5` 打包必失败 | CreateProcess 拒绝访问 | **传了目录而不是 python.exe**。`str(dir)` 得到文件夹路径，CreateProcess 报「拒绝访问」，看着像权限/杀软问题，其实是参数错 |
| embeddable 没有 pip | import site 失败 | 要重写 `python3xx._pth` 并加 `Lib\site-packages` |
| 第二次启动失败 | 配置在但端口没起 | 「配置存在」不等于「进程在跑」，要看端口 |

**真机验证记录（2026-10-22）：**
- 双击 `start.bat`（无任何参数）→ 自动发现 QQ `1741473279` → 29 好友
- 界面 16KB 正常渲染，Playwright 截图确认
- 配置文件正确写到 `%APPDATA%\markovcryptor\client.json`
- 全量测试 **507 passed**

### 交付包现状

| 包 | 内容 | 用途 |
| --- | --- | --- |
| `dist/qqtest-0.3.0-20261021-0613/` | 测试端 + 旧 wheel | probe / alice-bob 演示 |
| `dist/client_build9/qqclient-0.3.0-20261022-1740/` | **正式客户端**，零安装 | 双击 start.bat |

客户端包还没打 zip（等阶段 6 做完一起）。

---

## 阶段 5.5：跨子系统审查（2026-10-22）

实机测试前做了一次全面审查，覆盖并发、错误路径、资源生命周期。发现的都是
**只在多线程/异常路径下才出现**的问题，功能测试全绿也看不出来。

### 已修（按严重程度）

| # | 问题 | 后果 | 修法 |
| --- | --- | --- | --- |
| C1 | `Conversation.entries` 无锁，`_merge_history` 用「先快照再切片赋值」 | **丢消息**：快照与赋值之间到达的消息被整体覆盖抹掉；迭代时前插会**重复渲染**同一条 | 加 `_STATE_LOCK`；新增 `snapshot()` 供外部迭代；`prepend_history()` 在锁内一次完成去重+插入 |
| C2 | `unread += 1` 与 `mark_read()=0` 竞争 | 用户正看着窗口却亮红点，或丢一次未读 | 计数与 `_open` 判断放进同一临界区 |
| C3 | `Conversations._items` 迭代时被 `ensure()` 插入 | `RuntimeError: dictionary changed size`；并发 ensure 为同一 peer 建两个 Terminal，会话对象和实际收包的 Terminal 不是同一个 | 创建过程整体持锁（`_create_locked`） |
| C4 | `PictureQueue.take()` 判空与 `pop(0)` 分两次 | 单元素队列并发时 `IndexError`；`replenish()` 竞争会把同一张图入队两次 | 判空+pop+补充放同一临界区 |
| C5 | `Account.close()` 不停网络；`cli.py` 的 client 分支无 finally | 关闭后 OneBot 线程和 websocket 继续跑，往刚清空的结构投消息 | `close()` 里调 `network.stop()`；cli 加 `finally: account.close()` |
| E1 | `app.py` 只按 `ValueError` 映射，丢弃 `StegoError.http_status` 和 `hints` | 图片太小（413）、格式不支持（415）都报 **502**，用户去查网络；「怎么办」被丢掉 | `_http_error()` 统一转换，保留 hints 拼进 detail |
| E2 | 前端 `refreshState` 两处 `catch` 静默 return | 服务端一挂，界面永远停在旧状态且**毫无提示** | 状态变化时 toast 一次（不刷屏），恢复时也提示 |
| E3 | `_log` 只写 stdout | **pythonw 无控制台**，启动失败双击后什么都不发生 —— 用户只会认为包是坏的 | 同时写 `%APPDATA%\markovcryptor\client.log`；start.ps1 失败时显示日志末 12 行 |
| E4 | 队列空的提示不带 hints | 只说「队列为空」，不说怎么办 | 改 `InvalidInput` + hints（「指定图片文件夹或点补图」） |

### 未修（记录在案，不影响当前使用）

- `ask()` 的 `_ask_seq` 非原子自增 —— 并发调用可能撞 echo。当前前端是单请求
  串行轮询，实际不会触发；若将来并行调用需加锁。
- `_write_loop` 用 `run_in_executor` 阻塞 `queue.get`，重连时可能漏一个 executor
  worker。影响重连场景，日常不断线不触发。
- `Terminal.pending_request` 跨线程读写（握手自动回复 vs 补图重试）。两者同时
  发生才会重复回复，概率极低。
- `_merge_history` 吞掉历史读取异常（返回 200 但无历史）—— 已改成只合并一次，
  但失败时用户仍不知情。低优先级。

### 测试方法上的一个教训

**并发 bug 不能只靠压测验证。** 我一开始用「4 线程 × 300 次」验证 `take()` 的
竞态，把锁去掉后**测试依然全绿** —— GIL 下那个窗口太窄，而且 `replenish()`
自己也会加锁、不断补满队列，窗口几乎不存在。假绿的测试比没测试更糟。

改用**确定性验证**：
- 丢消息：直接构造「快照 → 插入消息 → 切片赋值」的时序，断言旧写法确实丢
  （`test_old_style_merge_would_lose_the_message`），作为"为什么不能那样写"的记录。
- 队列临界区：用记账锁替身记录 acquire 深度，断言 `pop` 发生时深度 > 0
  （`test_take_checks_and_pops_inside_one_critical_section`）。去掉锁后立刻 FAIL。

### 测试数

**527 passed**（审查前 507，新增 20 项）。

### 界面修正：消息归属与时间戳（2026-10-22，用户反馈）

用户反馈：消息栏看不出是发出的还是收到的。

**根因**：历史合并把每条都硬编码成 `mine=False`，于是用户自己发过的话
也排在左边、和对方一模一样。CSS 的左右对齐一直是对的，错在数据。

**修法**：
- `messages.py`：`ParsedMessage` 加 `at`（QQ 的 `time` 字段，范围校验，
  不在 2001~2286 之间当没有）；归属优先取 `sender.user_id`（历史条目
  里有，实时事件里没有）
- `app.py::_merge_history`：`mine=bool(account.uin) and item.sender == account.uin`
- 前端：气泡三重区分（**位置** 左右对齐 + **颜色** 蓝/灰 + **尖角方向**），
  气泡上方加「我 · 19:39」标签；历史跨天时时间戳自动带日期
  （一串 09:15 分不清是哪天）

**真机验证**：三历史消息全部正确标为我方（它们确实是用户发的）；
Playwright 构造对方消息确认左右布局 + 时间戳渲染正常。截图两张。

**测试**：`HistoryAttributionTest` 3 项，注入「硬编码 mine=False + 丢时间」
验过能 FAIL。全量 527 passed。

---

## 阶段 6：文本模型 ✅（2026-10-22）

### 设计回顾（为什么这样做）

文本信道**生成**中文载体而非改写已有文本，模型就是载体本身。双方必须各有
模型，接收方还要知道**是哪个模型**写的才能解。

「不需要协商」已经由帧校验实现了：`codec.py` 用模型指纹做帧校验的密钥，
解码端把手里每个模型都试一遍，对的那个会验证通过。**线上不传任何模型 ID。**

### 新增 `markovcryptor/client/models.py`

**ModelLibrary —— 懒加载**
- `scan()` 只列文件名，**不付加载代价**（启动时只看文件名）
- `load_first()` 同步加载一个（够发送，0.1~0.5s），`load_in_background()`
  加载其余（识别需要全部模型；后台加载期间到达的消息当作普通文本 —— 这与
  「对端用了我们没有的模型」是同一种情况，静默即可）
- 坏文件记一次就不再重试；`load()` 对失败过的 entry 返回 None（**真 bug**：
  原来返回 entry，调用方会以为加载成功）
- `identify(covertext)` 返回写入该文本的模型；普通文本和无模型都返回 None，
  调用方必须同等对待 —— 两种情况都不回复

**MultiModelTextChannel —— 多模型信道**
- 发送用首选模型；接收**逐个试所有已加载模型**
- `extract_text()` 对普通文本/未知模型返回 `b""`，**绝不抛异常**：
  不匹配是正常情况，不是错误（CLIENT.md §7.1 铁律）

### 接线

- `Account` 接受 `models=ModelLibrary(...)`，`start()` 里同步载一个、后台载其余
- `run.py::_model_library()`：先找配置的目录，再找 exe 旁的 `models/`；都没有
  返回 None（文本信道关闭，只能发图片密文 —— 这是合法配置，不是错误）
- API：`GET /client/models`、`POST /client/models/reload`（重新扫描）、
  `POST /client/models/select`（选发送用模型；收方向永远试全部）
- 界面：第二行工具栏「模型 4/4」+ 下拉选择 + 重新扫描；
  下拉只在名字变化时重建，否则 2.5 秒轮询会重置用户选择

### 实测数据（本机）

- 4 个模型全部加载 **0.51s**（比之前记录的单模型 0.53s 快，是热缓存）
- 编解码往返一致；普通文本正确返回空
- **跨模型识别成功**：用模型 A 编码、发送端切到模型 B，接收端仍能解出
  —— 「不需要协商」实测成立
- 真机：包内 `models/` 目录自动找到，4/4 加载，界面显示「文本信道可用」

### 测试

`tests/test_client_models.py` 23 项 + `ModelsEndpointTest`/`ModelsWithLibraryTest`
11 项。注入「解码只用首选模型、不做跨模型识别」验证能 FAIL
（`test_sender_switching_model_still_decodes`）。

**全量 560 passed**。

### 待办（阶段 7 之前）

- [ ] GUI 的模型训练 / 导入 / 导出（现有 CLI 能力：`markovcryptor train`）
- [ ] 客户端包打 zip（含 models/）
- [ ] 双机验证：模型 A 的机器发、只有模型 B 的机器收（应静默）

---

## GUI 技术债清单（阶段 6.5 之前审视，2026-10-22）

在做 GUI 必备功能之前，先把已知的设计债列清楚。这些不是 bug，是会让后续
每加一个功能都变难的结构问题。

### T1. 全量重渲染 + 2.5 秒轮询

`renderList()` 和 `renderConversation()` 每次都 `innerHTML = ""` 然后重建整棵
DOM。配合 2.5 秒轮询，等于每 2.5 秒把整个会话窗重建一次。

**后果**：
- 用户正在滚动查看历史，轮询一来**滚动位置被重置**
- 用户选中文本复制到一半，DOM 被换掉，选区丢失
- 图片每次重建都重新解码（虽然浏览器有缓存，但布局会闪）
- 消息一多（几百条）每次轮询都是 O(n) 重建

**正确做法**：增量渲染。按 `seq` 记录已渲染到哪一条，只 append 新的。
这是加任何"历史消息 / 更多功能"之前必须解决的，否则消息越多越卡。

### T2. 轮询而非推送

2.5 秒延迟意味着对方发来消息，最长要等 2.5 秒才出现。单机单用户够用，
但这是"能用"而非"好用"。

**已知取舍**：不上 WebSocket 是为了零依赖。但如果要做 SSE（`text/event-stream`），
浏览器原生支持，不需要任何库，服务端只是多一个 yield 循环。**优先考虑 SSE。**

### T3. 无滚动位置保持

配合 T1，即使不做增量渲染，也应在重渲染后恢复 `scrollTop`（且只在用户本来
就在底部时才自动滚到底——用户往上翻历史时不该被强行拉回底部）。

### T4. 错误处理分散

每个 `catch` 各写各的 toast。有的带重试提示，有的不带。没有统一的地方决定
"什么错误该持续显示、什么该一闪而过"。

**应该**：一个 `showError(scope, message)`，同类错误去重、可关闭、
严重错误常驻。

### T5. 无键盘可达性 / 无 aria

好友列表行是 `div.onclick`，键盘 Tab 不到、回车不能选。
`aria-selected` 只用在页签上。

**应该**：列表行用 `<button>` 或加 `tabindex` + `keydown`；
未读红点有 `aria-label`；消息区有 `role="log"`。

### T6. 模型训练/导入/导出全缺

CLI 有 `markovcryptor train`，GUI 完全没有入口。用户不可能去敲命令行。

### T7. 无「设置」入口

图片文件夹、队列上限、模型目录只能改 `%APPDATA%` 的 JSON 再重启。
这是必备功能，不是可选。

### T8. 无空状态引导

首次启动：好友列表空、队列 0/8、模型 0/0，界面上没有任何一句话告诉
用户"下一步该干什么"。

### 优先级

必修（本次做）：**T1 + T3**（否则消息一多就没法用）、**T6**、**T7**、**T8**
可做：T2（SSE）、T5
记录待办：T4 的统一错误层

---

## 阶段 6.5：GUI 必备功能 + 技术债（2026-10-22）

按 T1~T8 清单逐项处理。

### T1+T3 增量渲染与滚动保持 ✅

**这是所有后续 UI 工作的地基。** 原来 `renderConversation()` 每 2.5 秒把整个
会话窗 `innerHTML=""` 重建一遍 —— 用户滚上去看历史会被拽回底部、复制的选区
丢失、消息越多越卡。

改法：`Entry` 的 `seq`（全局单调计数）随 API 输出；前端按 `seq` 记住渲染到
哪，只 append 新的。整窗重建只发生在三种情况：切会话、历史被前插
（`R.firstSeq` 变小）、条目数变少。滚动只在「用户本来就贴着底部」时才跟随
（`nearBottom()`，阈值 80px）。

### T7 设置面板 ✅

齿轮按钮 → 弹窗（遮罩点击/Esc 关闭）。「常规」页：

- **图片文件夹列表**：可添加（原生对话框或粘贴路径）、移除；「无可用图片」
  的文件夹标黄 —— 区分「没配」和「配了但没图」，用户该做的事不同
- **队列上限** / **取图方式**
- **模型目录**：改完立即重扫描，不用重启
- 显示**配置文件完整路径**（用户有权知道程序把东西写到了哪）

保存后**立即生效**：队列原地重建、模型库原地替换。给新用户一句
「重启才能生效」是坏体验，尤其是对他们最可能做的第一件事。

### T6 模型管理 ✅

「文本模型」页：列表（名称 + 已加载/加载失败 + 导出 + 删除）+ 训练 + 导入。

- 训练：选语料文件（可浏览）→ 起名 → 同步训练（几 MB 几秒）→ 落盘 → 列表刷新
- 导入：复制 `.json` 进模型目录
- 导出：复制到用户选的目录
- 删除：**只允许删模型目录内的文件**，目录外一律 409 —— 本地 API 不能因为
  界面要求就删任意路径
- 失败的模型也显示（「加载失败」），否则用户永远不知道为什么少了一个

**删模型修了个真 bug**：后台加载线程握着文件句柄时 Windows 上 `unlink`
报 WinError 32。用户「刚训练完就删」必然撞上。加 `ModelLibrary.wait_idle()`
让删除前等加载放手；注入「不等待」验证测试稳定 FAIL 三次。

### T8 空状态引导 ✅

首次启动界面不再只有一句「选择一个好友开始」，而是三步清单：
图片文件夹（未配置/已配置 n 个）、文本模型（未加载/已加载 n 个）、
「去好友列表点一个人」。每一项的状态实时更新，旁边就是「打开设置」按钮。

### T5 键盘可达性（部分）

Esc 关闭弹窗；遮罩点击关闭。列表行仍是 div.onclick（未改，记录在案）。

### 一个差点翻车的事：patch 脚本把 JS 写坏了

用 Python 脚本往 index.html 里注入代码时，反斜杠被多重转义，写出
`join("` + 真换行 —— 整个 `<script>` SyntaxError，**页面空白且无任何提示**。
类似地还写出过四个连续反斜杠。

教训与防护：
1. 大段注入代码不要用 shell heredoc（会被 bash 吃一层转义），用写临时
   Python 脚本的方式
2. `node --check` 是唯一可靠的验证；把它做成测试
   （`WebuiSyntaxTest`：字符串未闭合 / 四反斜杠 / 真解析三关）

### 实测记录

- Playwright 全流程：空状态引导 → 打开设置 → 切到模型页 → 填语料路径 →
  训练 → 列表 4→5 → 新模型可选 → `models/select` 切换成功
- 训练出的模型落盘在模型目录，可用于文本密文发送
- 设置改动实时生效（换图片文件夹后队列立即重取样）

### 测试

新增 `tests/test_client_settings.py` 21 项 + `WebuiSyntaxTest` 3 项 +
`DeleteWhileLoadingTest` 1 项。全量 **585 passed**。

---

## 交付前修复（用户实测反馈，2026-10-22）

用户在真机上发现两个问题。都是功能测试全绿也看不出来的 —— 因为都涉及
**运行环境与测试环境的差异**。

### 问题 1：点开好友看不到聊天记录

**症状**：界面正常打开，点好友后消息区一片空白。后端明明返回了 21 条。

**根因有两层**：

1. `prepend_history()` 没给合并进来的历史条目分配 `seq`，它们全是默认值 0。
   前端增量渲染的判断是「seq <= 已渲染到的 seq 就跳过」，于是 21 条全部被
   当成"已渲染过"跳过 —— **一条都没画出来**。
   （这是我加增量渲染时埋的：写 `prepend_history` 时只考虑了并发安全，
   忘了它创建的条目也要遵守 seq 的约定。）

2. 历史里的**纯图片消息**没下载图片，合并后变成完全空白的条目 —— 就算修了
   seq，这些条目也只是"空着"，用户看起来仍像消息丢了。

**修法**：
- `prepend_history()` 在锁内给历史分配 `0..n-1`，现有条目重新编号到其后
  （历史在时间线最前，seq 必须比已有消息小，否则排序错乱）
- `_merge_history` 对纯图片消息加一行「对方发送了 图片」占位 —— 不下载图片
  是对的（20 条历史可能带几十张图），但不能让消息静默消失

**测试**：`HistorySeqTest` 3 项（seq 递增/不重复/低于已有消息/全部带 seq）+
`HistoryImagePlaceholderTest` 2 项。注入「不赋 seq」验证 FAIL。

**真机复验**：20 条全部渲染，跨天日期、我方/对方气泡、图片占位都正确。

### 问题 2：选择文件夹按钮无效

**症状**：设置里点「添加文件夹…」，什么都没发生，没有任何报错。

**根因**：`_pick_folder` 用 tkinter 的 `askdirectory()`。tkinter 必须在创建
Tk 根窗口的那个线程上运行，而这个函数跑在 **uvicorn 的工作线程**里 ——
对话框**永久阻塞**，HTTP 请求永不返回。按钮看起来就是"无效"，且控制台
没有任何错误。

测试环境抓不到它：单测里直接调 `_pick_folder`，tkinter 在主线程能正常返回。

**修法**：改用 Win32 原生 `SHBrowseForFolderW`（ctypes，无主线程约束）。
细节：
- 调用前 `CoInitialize`（uvicorn 工作线程通常没初始化 COM）
- 用 `BIF_NEWDIALOGSTYLE` 拿新版对话框（有地址栏可粘贴路径）
- **`CoTaskMemFree` 释放返回的 pidl** —— 不释放就是每次点一次漏一块内存
- 用户取消返回 None，界面退回手填路径

**测试**：`FolderPickerTest` 3 项 —— 工作线程里必须在几秒内返回（不能阻塞）、
不得引入 tkinter、失败返回 None 而非抛异常。其中「不阻塞」这条用真线程
+ 10 秒超时验证。

**真机复验**：API 0.04s 返回（无头环境），不再挂起；有桌面时弹原生对话框。

### 教训：为什么功能测试抓不到

两个问题都源于**测试环境与真机运行环境的差异**：
- seq 问题：测试断言的是数据结构（条目数、归属），不是前端渲染结果
- tkinter 问题：测试在主线程里调 `_pick_folder`，正好避开了它的限制

防护：
- 历史条目现在有专门的 seq 测试（递增、不重复、全部带）
- `_pick_folder` 有"工作线程不阻塞"测试，明确验证线程语义

### 交付包同步

修复已同步进 `dist/final/qqclient-0.3.0-20261022-2302/`（app.py、
conversations.py、webui/index.html）。

**全量 593 passed**（新增 8 项）。

### 交付包最终状态（2026-10-22）

`dist/final/qqclient-0.3.0-20261022-2302/`（154MB）+ 同名 zip（58.8MB）

- 含全部最新修复（历史 seq、图片占位、文件夹选择器）
- 含 4 个文本模型、自带 Python/Node/SnowLuma
- **1167 个文件与 zip 逐字节 sha256 一致**（0 缺失/0 多余/0 差异）
- 使用说明：`docs/CLIENT_README.md`
- 过时半成品已按用户要求送回收站（343MB 失败残留 + 旧 client_build9）

### 剩余待办

- [ ] 双机真机验证（阶段 7）：本机已向 3925697820 发出真实握手，等待虚拟机上线
- [ ] 双机场景重点验证：模型 A 的机器发、只有模型 B 的机器收（应静默）
- [ ] T2（SSE 推送替代轮询）、T4（统一错误层）、列表行键盘可达性

---

## 界面修正第二批（用户实测反馈，2026-10-22）

### 问题 3：历史记录撑出窗口、盖住输入区

**症状**：消息一多，整个会话窗被撑高，历史气泡越过下方的队列栏/输入框，
一直到屏幕外。

**根因**：flex 项的默认 `min-height` 是 `auto`（"不小于内容高度"）。`.thread`
设了 `flex:1` + `overflow-y:auto`，但内容一多它就想更高，`overflow` 永远不
生效 —— 整条 flex 链（body→main→#chat→.panes→.pane→.thread）都各自被内容
撑开。CSS 看着每层都"对"，合起来就是不滚。

**修法**：整条链补 `min-height: 0`（flex 项才允许收缩到比内容矮），并给
`topbar`/`composer` 设 `flex: 0 0 auto`（固定高度，不被挤掉）、
`#chat` 从 `height:100%` 改成 `flex:1 1 0`。浏览器实测各层
`scrollHeight == clientHeight`，`.thread` 内部滚动生效（579 vs 内容 1340）。

这类 bug 纯看 CSS 很难发现 —— 每一层单独看都"没问题"。教训：**flex 布局
里凡是想让它滚动的元素，从最外层到最内层都必须 `min-height: 0`**，漏一层
就整体失效。

### 问题 4：右键菜单是浏览器的

**症状**：右键弹出"重新加载/检查/另存为/翻译"。这是浏览器网页的菜单，
出现在一个号称是应用（--app 模式）的窗口里，既没用也露馅。

**修法**：
- 全局 `contextmenu` 屏蔽默认菜单（**输入框除外** —— 那里系统菜单的
  复制/粘贴比我们做得好，不抢）
- 消息上右键 → 自定义菜单：「复制消息」+ 有密文时「复制密文」+
  有选中文字时「复制选中内容」
- 其他位置：只在有选中文字时给「复制选中内容」
- 菜单贴边自动翻转；点击/Esc/失焦/窗口缩放时关闭
- 顺带挡掉 F5 / Ctrl+R —— 应用窗口里重载会丢会话和滚动位置，用户没有
  任何理由需要它

复制用 `navigator.clipboard`，失败（file:// 或旧内核）退回
`document.execCommand`，不让"复制"按钮什么都不做。

### 真机复验

- 布局：`.thread` 579px 高、内容 1340px、内部滚动生效；输入框在视口内
- 右键消息 → 「复制消息」出现；空白右键 → 无菜单（或只有复制选中内容）
- 全量 593 passed

---

## 严重事故：交付包缺依赖（用户虚拟机实测，2026-10-22）

### 症状

用户在虚拟机双击 `start.bat`：

```
ModuleNotFoundError: No module named 'cryptography'
启动失败（退出码 1）。
没有日志文件。多半是包不完整。
```

### 根因：自检被打包机的环境污染了

包内 `python/Lib/site-packages/` 只有 `cffi`、`markovcryptor`、`pip` ——
**所有第三方依赖都没装进去**。

但打包时的「自检」是通过的。为什么？

因为包内 Python 启动时会读到**打包机的用户级 site-packages**
（`C:\Users\death\AppData\Roaming\Python\Python312\site-packages`），
`cryptography` 是从那里借来的。打包机上当然有，虚拟机上没有。

验证：
```
包内 python  → import cryptography 成功（位置在 AppData\Roaming\...）
包内 python -I → ModuleNotFoundError
```

**这是最恶劣的一类 bug**：自检"通过"、包看起来是好的，直到换一台干净
机器才炸。而且失败信息（「多半是包不完整」）恰好指对了方向，但没有任何
东西告诉我们缺哪个。

### 修法（三层）

1. **自检必须隔离**：`python -I` + 清掉 `PYTHONPATH` / `PYTHONHOME` /
   `PYTHONUSERBASE` / `PYTHONSTARTUP`，并设 `PYTHONNOUSERSITE=1`
2. **自检要 import 真实依赖**：`numpy, PIL, reedsolo, cryptography,
   fastapi, uvicorn, websockets` —— 只 import `markovcryptor` 不够，
   它自己会去借包
3. **新增 `_verify_site_packages()`**：直接查目录里有没有这些包，
   **不靠 import**。import 成功可能来自宿主机，查文件不会骗人

第 3 层立刻抓到第二个问题：`reedsolo` 是**单文件模块**（`reedsolo.py`），
我的检查只找同名目录，误报缺失。改成同时认 `.py` 和 `*.pyd`。

### 顺带发现：wheels 目录曾丢失

`dist/qqtest-*/` 一度不在磁盘上（在回收站里），已还原。
教训：`dist/` 下的东西即使是"构建产物"，也可能是唯一副本。

### 结果

- 包体 140MB → **237MB**（多了 97MB 依赖，这才是正常的）
- zip 92MB，3951 个文件逐字节 sha256 一致
- zip 内确认含 `cryptography/`、`numpy/`、`reedsolo.py`、`PIL/`
- 真机启动正常：识别 QQ、加载模型、界面可用

### 测试

`BundleDependencyTest` 5 项：自检用 `-I`、清 PYTHON 环境变量、
import 真实依赖、查文件而非只 import、认单文件模块。
注入「去掉 -I」验证 FAIL。全量 **598 passed**。

### 教训

**「在打包机上验证通过」不等于「包是好的」。** 打包机的环境总是比目标机器
丰富。凡是自检，必须在尽可能接近目标机器的环境里跑 —— 做不到就显式隔离。

---

## 重握手（Rekey）语义修正（2026-10-22）

### 问题

用户提出：*"由于我们的通信无状态，应支持对方随时用新的密钥重新握手"*。

实测发现**已有的信道会被一次重握手打死**：

```
建立: alice=已建立 bob=已建立
无图重握手后: alice=等待对端回复 bob=等待对端回复
  alice.pending_request: True
InvalidInput: 会话等待对端回复，不能发送密文
```

`Session.absorb()` 收到 `Mode.REQUEST` 时**无条件**覆盖 `peer_public` 并把
state 改成 `WAITING`。于是对端一重握手，本端立刻失去可用密钥 —— 而本端可能
正握着待发图片队列为空，回复发不出去，两边永久卡在"等待对端回复"。

**根因是概念错误**：把"收到重握手请求"当成了"密钥已经更换"。实际上在对方
拿到我们的回复之前，旧密钥对双方都还有效。

### 修法：暂存（staging）+ 原子切换

`Session` 新增暂存字段 `_staged_private_key` / `_staged_my_public` /
`_staged_peer_public` / `_staged_outgoing`，以及 `rekeying` 属性。

规则：
- **已建立时收到 REQUEST** → 只暂存对端新公钥，**保持 ESTABLISHED**，
  旧密钥继续收发
- **`begin()` 在已建立时** → 只生成并暂存自己的新密钥对，不动现役密钥
- **`answer()` 不再修改现役会话**，只准备材料（`_staged_outgoing`）；
  调用方在**报文真正发出后**才调 `commit_answer()` 落盘
- 嵌图失败时调 `discard_answer()`，现役会话毫发无伤
- 新握手完成时 `generation` 才 +1

`_auto_respond()` 相应改成"发送成功后 commit"。`_on_handshake` 在重握手挂起时
提示"（旧密钥仍可用）"，避免用户误以为信道断了。

### 为什么 answer() 必须两阶段

回复装在图片里，而图片可能装不下 38 字节握手。原来 `answer()` 立刻 `_derive()`
—— 密钥换了、回复却没发出，对端还在旧密钥上，**双方永久错位**。
拆成 `answer()` + `commit_answer()` / `discard_answer()` 后，只有发送成功才切换。

### 实测

| 场景 | 结果 |
| --- | --- |
| 重握手 + 本端无图 | 双方仍「已建立」，密文照常收发 ✓ |
| 补图后 `retry_pending` | `generation` 1→2，双方同步，双向密文通 ✓ |
| 连续 3 次重握手 | 每轮都可用 ✓ |
| 回复因图太小失败 | `generation` 不变，旧会话完好 ✓ |

测试：`RekeyTest` 4 项。注入「收到 REQUEST 立刻覆盖密钥」验证 2 项能 FAIL。
修正了 `test_fresh_handshake_means_old_body_fails`：旧密文失效的时机是重握手
**完成**时，不是收到请求时。

### 顺带：全量回归提速

`--durations` 显示单条测试吃掉 196 秒：

```
196.54s  test_qq_tools.py::PackagingTest::test_generated_wheels_cover_declared_targets
```

它真下载 cp311/cp314/cp315 的 numpy/pillow wheel，占全量 63%，与日常开发无关。

处理：`pyproject.toml` 加 `slow` marker 且 `addopts = "-m 'not slow'"`，
给两条真联网/真打包的测试打上标记。发版前用 `-m slow` 单独跑。

**310 秒 → 106 秒（快 3 倍）**，623 passed（2 条 slow 默认跳过）。

---

## 交付包隐私与体积治理（2026-10-22）

### 问题 1：包里带着打包机的账号信息

用户指出：*"打包文件中不应包含填入具体账号信息的 snowluma 配置文件及其他涉及
个性化的配置项"*。

审计发现包里确实有：

| 文件 | 内容 |
| --- | --- |
| `snowluma/config/onebot_1741473279.json` | **账号 + OneBot 明文 accessToken** |
| `snowluma/config/webui.json` | WebUI 密码哈希 + 盐 |
| `snowluma/data/1741473279/messages.db` | 聊天记录 |
| `snowluma/data/1741473279/snowluma_identity.db` | 身份库 |
| `snowluma/logs/*.log` | 说过的话 |

根因：我手动同步代码时把整个 `snowluma/` 拷进了包（打包脚本本身只建空
`config/`，是对的）。

修法两层：
1. `_scrub_personal_state()`：清空 `config/`、删掉 `data/` `logs/` `cache/` `tmp/`，
   挂在 `_copy_snowluma()` 里
2. `_verify_no_personal_state()`：对**成品包**做最终扫描，发现任何
   `onebot_*.json` / `webui.json` / `runtime.json` / `snowluma/data/` /
   `snowluma/logs/` / `snowluma` 下的 `.db` 就直接让构建失败

为什么必须做成"失败"而不是"警告"：这些文件一旦发出去，对方的包里有别人的
账号和令牌，而且**不可从包本身恢复** —— 属于静默泄漏。

### 问题 2：体积冗余

用户问：*"是否包含其他可能冗余的文件、可裁剪的内容或者仍然可以压缩体积的
方式？model 似乎也不必随包附带"*。

审计结果（裁剪前 237MB）：

| 项 | 大小 | 处置 |
| --- | --- | --- |
| `pip` | 12 MB | 删（只用来装东西，装完无用） |
| `__pycache__`（182 个） | 29 MB | 删（Python 会按需重编） |
| numpy 的 tests/f2py/distutils | ~4 MB | 删（运行时不引用） |
| `models/`（4 个模型） | 11 MB | **删，改成空目录 + 说明** |
| `node.exe` | 89 MB | 保留（SnowLuma 硬依赖） |

**关于 models/**：用户的判断是对的。模型是拿自己的语料训练的，别人的模型对他
没用；而且这些模型来自有版权的书，附带分发还有法律问题。改成 `models/README.txt`
说明"为什么是空的、怎么获得、要不要和对方用同一个模型"。

实测无模型时的行为符合预期：日志「没有找到文本模型；只能发送图片密文」，
界面「模型 0/0 / 未配置模型目录（只能发送图片密文）」，图片密文照常可用。

**关于 node.exe**：89MB 是单项最大头，deflate 可压到 33MB。评估过 SEA
（Single Executable Application）方案，但需要改 SnowLuma 的入口方式，风险高于
收益 —— 而用户实际下载的是 zip，压缩由 zip 完成，包内目录大小不影响交付体积。

裁剪安全验证：删完后在隔离模式（`-I`）下逐个 import numpy/PIL/reedsolo/
cryptography/fastapi/uvicorn/websockets/markovcryptor，并实测 X25519 生成、
PIL 编码、numpy 运算，全部正常。

### 结果

| | 之前 | 现在 |
| --- | --- | --- |
| 包目录 | 237 MB | **209 MB**（裁剪 34 MB，含新 wheel） |
| zip | 92 MB | **77 MB** |
| 账号残留 | 有（token/密码哈希/数据库/日志） | **无** |
| models/ | 4 个第三方模型 11MB | README.txt 说明 |
| 文件数 | 3951 | 2159 |
| 全量回归 | 106 秒 | **94 秒** |

zip 与目录逐字节 sha256 一致（2159 文件，0 缺失/0 多余/0 差异）。

### 测试

`BundlePrivacyTest` 7 项：清理函数删除账号配置/数据库/日志但保留 config 目录；
校验函数拒绝残留、放行干净包、忽略 site-packages 里的同名文件、抓得住散落的
`.db`；**构建流程真的调用了这两个函数**（只写函数不挂进流程，包照样发出去）；
脚本里不出现任何真实 QQ 号。

### 教训

**「构建产物」不等于「可以直接分发」。** 打包机是跑过真账号的机器，它的
`config/`、`data/`、`logs/` 都是个人数据。清理必须做成构建流程的强制步骤 +
成品扫描，而不是靠人记得手动清。

---

## 界面重构：并列输入单元 + 密文区状态机（2026-10-22，待用户调试）

按用户详细规格重做会话窗布局。**这批改动还没跑全量回归** —— 用户明确要求
UI 调试无误后再打包，所以只做了针对性验证（JS 语法检查 + Playwright 实测）。

### 布局：明文/密文是两个并列的「显示+输入」单元

原来四个区（明文线程、密文线程、共享输入框）是错开的；现在每栏自带输入区，
明文和密文各自是完整的输入输出单元。

中间加了一条 **6px 的可拖动分隔条**：
- 拖动改左右占比（存 localStorage —— 界面偏好不是聊天数据，落盘无妨）
- 双击恢复均分
- 某一侧窄于 220px 时**自动隐藏那一侧**，剩下一栏占满；拖宽即恢复

### 密文区状态机（四态遮挡层）

| 状态 | 遮挡 | 内容 |
| --- | --- | --- |
| `locked` 未握手 | 有 | 🔒「尚未握手」+ 大按钮「发起握手」 |
| `waiting` 已发起 | 有 | ⏳「已发起第 n 次握手」+「再发起一次握手」 |
| `noimage` 图片不足 | 黄色警告 | 「图片数量不足」+ 原因 +「补图」按钮 |
| `ready` 已握手 | 无 | 露出输入区，正常收发 |

关键点：
- **待发图片队列永远浮在遮挡之上**（`.queuefloat`，z-index 30 > 遮挡 20）。
  用户明确要求握手前就能查看和管理待发图，所以它不能被挡
- `locked`/`waiting` 时输入框禁用 —— 这个状态除了等对面握手，能做的只有
  发起握手
- `waiting` 可以反复点：对方没收到包或没开终端时重发是正当需求
- `noimage` 用黄色而非灰色：这是"能修的状态"（补图/换文本信道），不是"被锁"

### 图片/文本信道开关

左右式 switch 控件，默认图片、可切文本：
- **无模型时不能切到文本** —— 切过去也发不出去，那是骗用户。开关保持图片位
  置，旁边显示「⚠ 未配置文本模型」（而不是隐藏下拉菜单 —— 用户需要知道
  为什么不能切）
- 重新配好模型后自动恢复：`refreshState` 每轮都把模型状态喂给渲染函数
- 文本信道发密文**不消耗队列图片**（新增 `Account.send_covert_text`），
  这是它存在的意义：只剩一张图时，短消息走文本信道，图留给握手

### 图片容量预判（`Account.covert_capacity`）

逐张量队列里每张图能装多少字节（复用终端的容量算法，GIF 和静图分别算），
累计后和"要藏的内容"比较：
- 握手固定 38 字节（从协议实现里量出来的，不是抄常量）
- 正文按「帧头 + nonce + UTF-8 3 字节/汉字 + 96 字节余量」估

`/client/state` 带 `handshake_capacity`，界面据此在**用户输入之前**就提示
"图片数量不足"，而不是等发出去了才报错。

文件夹变了怎么办：队列的补图逻辑每次都重新扫文件夹（`replenish`），用户加了
图点「补图」即可；图片从队列移除后下一轮 state 会重新算容量并更新遮挡。

### 明文栏支持图片

- **粘贴**（Ctrl+V）和**拖入**都接住了：`paste` 事件里取 `clipboardData.items`
  里的 file，`dragover/drop` 处理拖拽（dragover 必须 preventDefault，否则
  浏览器会直接打开图片）
- 附件显示为一排缩略图，每张右上角 × 可移除
- **Enter 发送时文本和附件合并为一条消息**；文本为空但有图也照发（QQ 允许
  只发图）
- 缩略图在浏览器端用 canvas 生成（54px），不为预览把原图传一遍服务端
- 上限 9 张 / 单张 8MB，超了给明确提示

后端 `/client/send` 相应支持 `images`（data URL 数组，服务端解码并校验大小）。

### 实测（Playwright，全部通过）

- 未握手：遮挡 + 大按钮 + 队列浮层可见（z-index 30/20）
- 点「发起握手」→「已发起第 1 次握手」/「再发起一次握手」
- 分隔条拖动 557→336px 生效；双击恢复
- 无模型时开关不可切、显示「⚠ 未配置文本模型」
- 粘贴 2 张图 → 附件 2 → 提示行更新 → 发送后清空
- 换成只有 64×64 小图的文件夹 → 黄色「图片数量不足」+「补图」
- 信道建立后（对端在跑旧版终端）遮挡消失、密文区可用

### 已知待调试项（等用户反馈）

- 遮挡层的透明度/颜色是否合适
- 220px 的自动隐藏阈值是否合理
- `noimage` 状态下是否该把文本信道直接切过去（现在是提示但不强切）
- 明文附件拖入时整个 composer 高亮，视觉是否够明显

---

## 启动修复 + 消息归属 + 拖入模型（2026-10-23）

### 严重：重启后无法启动（用户实测）

用户虚拟机重启后双击 `start.bat`：

```
发现已登录的 QQ 3925697820，启动 SnowLuma…
启动失败：OneBot 端口没起来：ws://127.0.0.1:3001/
```

**根因**：`run.py` 用上次留下的 `config/onebot_<QQ>.json` 里的端口去等
OneBot，但 **SnowLuma 每次启动都会重新分配端口**（3000/3001）。重启后配置里
写着 3001、实际监听 3000，代码就死等 3001 直到超时。

旧注释写着"配置存在 ≠ SnowLuma 在跑"，这个判断是对的；漏掉的是**另一半**：
配置里的端口也不可信。

**修法**：旧配置只用来"猜一下是不是已经在跑"（`wait_for_onebot(timeout=1.0)`）。
凡是我们自己启动了一份，就必须**重新发现**端口 —— 启动后调
`wait_for_endpoint()` 重读配置。

同时把 `run()` 的后半段（连 QQ、起 uvicorn）抽成 `_serve()`，这样"选哪个端口"
这件事可以单独测试，不必真开 socket。测试从 88 秒降到 3.6 秒。

测试 `PortRediscoveryTest`（2 项）：注入"启动后不重新发现端口"验证能 FAIL。

### 消息归属与占位（用户要求）

用户指出两点：

1. **纯图片消息不该再显示"对方发送了 图片"** —— 图已经完整显示出来了，
   旁边再写一行是重复且荒谬的
2. **我方发出的未能解析消息**（例如从其他平台发的语音）也要用我方气泡展示，
   带同样的小字提示

修法：
- `ParsedMessage` 加 `mine` 字段；`parse(payload, self_uin=...)` 在解析时就算出
  归属，人称按它选（`"我发送了"` / `"对方发送了"`）
- `describe_unsupported(kind, mine=)` 同样按人称
- `_merge_history` 只在**拿不到 URL**（真的看不到）时才加
  `"{who} 图片（无法加载）"`；能下载就什么都不加
- `Conversations.attach_self(uin)`，`Account.start()` 里接上

测试 `SelfAttributionTest`（4 项）+ 改写 `HistoryImagePlaceholderTest`（2 项，
原来那条断言的正是要改掉的旧行为）。注入"人称写死"验证能 FAIL。

### 拖入 .json 安装文本模型（用户要求）

用户要求：把模型 json 拖进窗口 → 认出是文本模型 → 直接加载并存入运行路径下
的默认模型文件夹，省掉"设置→选目录→拷文件→重新扫描"四步。

后端 `POST /client/models/upload`：
1. **先校验**：写到临时文件并 `MarkovModel.load()`。不是模型就 422 报
   "这不是可用的文本模型"，**不落盘** —— 否则用户会在模型列表里看到一条
   "加载失败"却不知道为什么
2. **再保存**到默认模型目录，然后 `scan()` + 后台加载

安全细节：文件名只取最后一段（`Path(name).name`），`../../evil.json` 会被削成
`evil.json`，不会写到目录外。

默认目录：打包形态下 `sys.executable` 是 `<包>/python/python.exe`，直接取它的
父目录会得到 `<包>/python/models` —— 那不是用户会去看的地方。所以优先用
`<包>/models`（有 README.txt 的那个），没有才退回解释器旁边。

前端：任意位置拖入 `.json` → 半透明提示层「松开以安装文本模型」→ 上传 →
toast「已安装模型：X」。与明文输入区的图片拖拽用文件类型区分，不打架。

测试 `ModelUploadTest`（5 项）：有效模型落盘+加载、无效 json 被拒且不落盘、
路径穿越被削平、无扩展名补 `.json`、空 payload 拒绝。注入"不校验模型"
验证能 FAIL。

### 实测

- 交付包真机：上传后保存到 `<包>/models/《叫我道祖》.json`，内容字节一致
- Playwright 模拟拖入：模型数 1 → 2，两个都加载成功，无 JS 错误
- 针对性回归：**311 passed**（`tests/test_client_*.py` + `test_terminal.py`）
- 按用户要求仍未跑全量，等 UI 定稿

---

## 前后端同生共死（2026-10-23）

用户反馈三件事，本质是同一个问题：前后端被当成两个独立的东西。

1. 关掉 GUI 后后端仍在跑，SnowLuma 还挂着
2. 后端关了，前端还显示「未连接」而不是一起退出
3. 「是否连接」这个 label 是多余的 —— 默认前后端一一对应
4. 后台黑框能不能在前端已启动后隐藏

### 新增 `markovcryptor/client/lifecycle.py`

- `LivenessWatchdog`：前端定时心跳，停了就退出。**起始不武装**（服务起来但
  浏览器还没连时不能因为"没心跳"就退），第一次心跳才武装。超时按分钟算 ——
  浏览器后台标签的定时器会被节流到 1 次/分钟
- `OwnedProcesses`：**只记我们自己起的进程**。用户手动开的 SnowLuma 不能
  因为关这个客户端就被杀掉。杀的时候用 `taskkill /F /T` 连带整棵进程树
  （`node index.mjs` 会派生子进程，只杀父进程会留孤儿占着端口 —— 正是用户
  报的那个症状）

### 双向绑定

- 后端 → 前端：`/client/events`（SSE）+ `/client/ping`；`/client/quit`
- 前端 → 后端：`pagehide` 时 `sendBeacon("/client/quit")`；另有显式退出按钮
- `run.py`：把 `uvicorn.run()` 换成 `uvicorn.Server` + 监视线程，这样才能在
  用户点退出时优雅收尾。`_make_shutdown()` 幂等，三条退出路径共用

### 退出延迟：6.3 秒 → 1.4 秒

一开始用 SSE 感知后端退出，**实测反而更慢**：后端进程被强杀时没有 FIN/RST
及时送达，浏览器要等 TCP 超时（实测 5 秒）才触发 `onerror`。

改回轮询，但要解决两个真问题：

1. **Chrome 对已死端口不会立刻回 ECONNREFUSED**，它会重试连接，单次耗时
   约 2 秒。所以给所有请求加了 `AbortController` 超时（1200ms）。没有这个，
   缩短轮询间隔完全无效 —— 一次失败的探测本身就比周期还长
2. **"连续两次失败才关窗"让延迟翻倍**。这个容错在本程序里没有意义：后端是
   自己起的，它退出就意味着会话结束。改成超时即判定

最终：轮询 1.5 秒一次，超时 1.2 秒，超时即退出。实测 1.4–1.5 秒。

### 界面

- 去掉「已连接/连接中」标签，改成固定的「本机」+ QQ 号
- 加退出按钮（⏻，带确认）
- `shutdownWindow()` **先改界面再尝试关窗**：普通标签页里 `window.close()`
  会被拒绝，只靠它用户会对着一个假界面发呆

### 启动脚本：黑框

`start.ps1` 改成先跑 2 秒预检（能 import 就过），通过后用 `pythonw.exe`
`-WindowStyle Hidden` 正式启动 —— 正常使用时没有黑框。失败时仍然用有控制台
的解释器跑一遍并打印日志最后 12 行，错误看得见。

### 测试 `tests/test_client_lifecycle.py`（17 项）

看门狗 7 项（不武装/武装/超时触发/有心跳不退/只触发一次/disarm/回调异常）、
OwnedProcesses 5 项（只记自己的/None 忽略/已退出不重复杀/杀整棵树/幂等）、
端点 5 项（quit 置位并回调、ping 返回间隔、SSE 路由存在、退出态发 quit 事件、
无 watchdog 时不崩）。注入「看门狗永不触发」验证 3 项 FAIL。

### 实测

- `/client/quit` → python 退出、node 退出、8741 关闭（全清）
- 杀后端 → 前端 1.4 秒后显示「已退出」并尝试关窗
- 「已连接」字样消失，退出按钮存在
- 针对性回归 **115 passed**（lifecycle + app + run + launcher）

---

## 分隔条可恢复 + 端口自动切换 + 滚动条美化（2026-10-23）

### 分隔条：收起后拉不回来（用户实测）

拖到一侧消失后，分隔条被 `hidden = true` 一起藏了，**没有任何办法把那一侧
拉回来** —— 而且ratio存在 localStorage 里，重启也一样。

修法：分隔条**永远不隐藏**。被藏的是那一侧的 pane；分隔条留在剩下那栏的
边缘上（`.at-left`/`.at-right`），带常显的白色横线抓手（repeating-linear-
gradient 画的三条小横线），title 也换成方向指引。拖它一下 ratio 离开极值，
那一侧自动重新出现。

ratio 的取值范围从 `0.05..0.95` 放宽到 `0..1` —— 极值本身就代表"收起这一侧"，
不应该被硬夹取丢弃。

### 端口占用：自动换 + 可配置

原来客户端界面端口写死 8741，被占就抛 OSError —— 用户看到的是没有任何提示
的启动失败，也不知道该改什么。

`lifecycle.find_free_port(preferred, tries=30)`：从首选端口往上找第一个能绑的。
用户可以在配置里把 `port` 钉死；没钉就从 8741 开始走。换端口时用
`netstat`/`tasklist` 查出**占用者的名字和 PID** 写进日志：

```
端口 8741 已被占用（被 python.exe (PID 363868) 占用），改用 8742
```

实测两个实例：第一个拿 8741，第二个自动用 8742 并报出占用者。

注：探测和 uvicorn 真正绑定之间理论上存在竞态（微秒级窗口），但 uvicorn
输了也会报清晰的错误，可接受。

### 滚动条美化（用户反馈）

默认的 Windows 滚动条在深色界面里是一块浅灰方条，还占 17px 宽 —— 侧栏本来
就窄，一出现整列跟着跳。

统一成细滚动条（8px、圆角、轨道透明、hover 变亮）：
- Chromium（`--app` 模式就是它）：`::-webkit-scrollbar` 系
- Firefox（降级路径）：`scrollbar-width: thin` + `scrollbar-color`

多行输入框加 `max-height: 132px`（约 6 行）+ `overflow-y: auto`，内容多了
自己滚，不再把整个 composer 顶高。

### 测试

- `PortSelectionTest` 5 项：首选空闲时用它、被占往上走、返回的端口真能绑、
  全占时报错、owner 查询不崩
- `SplitterRecoveryTest` 4 项 + `ScrollbarStyleTest` 3 项（静态断言，
  注入"收起时藏掉分隔条"验证 2 项 FAIL）

### 实测（Playwright）

- 拖到最左 → 明文栏收起、分隔条留在左边缘带抓手、title 提示方向
- 从边缘拖回 → 明文栏恢复 414px
- 端口 8741 被占 → 第二实例自动用 8742 并报出占用者
- 针对性回归 **87 passed**

---

## 消息重复 + 输入框高度 + 历史序号冲突（2026-10-23）

### 未打开会话时收到消息，点进去重复两遍（用户实测）

场景：会话没打开时对方来信 → 点进最近列表 → 同一条消息出现两次。

原因：消息到达时 `_append` 实时记了一条；点开会话拉历史，那条消息又从历史
里来一次，而 `prepend_history` **无条件整段插入**，没有排除已经在时间线里的。

修法：
- `Entry` 加 `message_id` 字段（来自 `ParsedMessage.message_id`）
- `prepend_history` 合并前先建两个集合：已有条目的 id、以及"内容+时间戳"
  指纹；历史条目命中任一就跳过
- 指纹只用于**没有 id** 的条目，且刻意包含时间戳 —— 两条不同时刻的"嗯"
  是两条真实消息，不能合并

### 输入框太窄（用户实测）

原来写死 `height: 46px`，只够一行半，写两句话就要靠滚动。

改成 JS autosize：`min-height: 46px` / `max-height: 168px`（约 7 行），
内容长高到上限才出滚动条。不用 CSS `field-sizing: content` —— 那是
Chromium 123+ 才有的新属性，而客户端要跑在 Win10 上。

`autosize()` 每次测量前先把高度归零，否则 `scrollHeight` 只增不减，元素会
卡在历史最大值。清空统一走 `clearInput(id)`（置空 + 收回），避免每处都记得
调 autosize。

### 历史序号与实时消息撞车（本地不显示）

这个是上一轮 `prepend_history` 修复的**反例**：原实现把已有条目重新编号成
`base..base+n`（base = 历史条数），而 `_append` 用全局 `_ARRIVAL`。两者互相
不知情，新发的消息拿到 6、7 这样的小号，重新编号后的老消息是 20..25 ——
seq 回退，前端 `seq <= lastSeq` 全跳过。

**消息确实发出去了（QQ 那边收到了），但本地界面不显示。**

修法：历史用**负数**（`-(len-1)..-1`），天然排在实时消息之前且永不与
`_ARRIVAL` 分配的正数冲突；已有条目一个都不动。

配套前端改动：`R.lastSeq` 的"未渲染"哨兵从 `-1` 改成 `null` —— 用 -1 会把
所有负数历史当成"已渲染过"跳过。

### 测试

- `HistoryDuplicateTest` 5 项：同 id 不重复、不同 id 都保留、纯历史全保留、
  无 id 走指纹去重、去重后 seq 仍单调
- `HistorySeqCollisionTest` 3 项：历史不压过实时、新消息 seq 最大、
  历史 seq 为负数且有序
- `TextareaAutosizeTest` 4 项

注入"不去重"验证 2 项 FAIL；注入"恢复旧的重新编号"验证 3 项 FAIL。

### 实测

- 交付包同款 SnowLuma，点开会话：20 条（此前 26 条含重复），seq 单调唯一，
  零重复，10 条带图
- 输入框 46px → 5 行时 132px，清空后收回
- 针对性回归 **177 passed**

### 教训

测试期间向真实 QQ 账号发过"测试发送""API 直测"之类的内容，对方真的会看到。
以后**不再向真实账号发任何测试消息**：验证走单元测试 + `InProcessOneBot`
假信道 + Playwright 只读检查。

---

## 头像放大 + 无系统标题栏（2026-10-23）

### 头像尺寸

换成真实头像后反而比原来的首字母圆小了一号：`.avatar` 34px、行内的
`.avatar.small` 只有 26px。放大一档：40px / 34px，字号跟着提。
另外查出一处**重复的 `.avatar` 规则**（旧的 accent 底色样式还留在文件后面，
把新尺寸覆盖了），已合并；自己的头像保留 accent 底以示区分。

### 去掉 Windows 自带窗体头

用户要求：`--app` 模式仍带系统标题栏，顶部第一行应该是我们自己的身份栏，
窗口控制（最小化/退出）放在它右侧。

两步：

**1. 独立 profile（`browser.py`）**

关键发现：不带 `--user-data-dir` 时，启动的 chrome 进程只是个**启动器**——
它把 URL 转交给用户已在跑的 Chrome 然后退出。窗口真正属于用户那个长期
运行的浏览器进程，根本没法和用户自己的标签页区分。

加上私有 profile（`%APPDATA%\markovcryptor\browser-profile`）后，进程真的
拥有自己的窗口，`Popen.pid` 就是窗口的属主。这一步不做，后面全免谈。

**2. Windows API 去边框（新 `client/window.py`）**

网页没有任何 API 能改自己的 OS 窗口。所以用 ctypes：
`EnumWindows` 按 pid 找到可见顶层窗口（带超时轮询，Chromium 起窗要几秒），
`GetWindowLongW(GWL_STYLE)` 清掉 `WS_CAPTION | WS_THICKFRAME | _WS_SYSMENU |
WS_MIN/MAXIMIZEBOX`（这些按钮我们自己画，留系统的会出现两套），再
`SetWindowPos(SWP_FRAMECHANGED)` 让样式生效。

放在后台线程跑（`_adopt_window`），不阻塞 uvicorn —— 用户在等窗口。
显式 `restype`/`argtypes`：默认 `c_long` 在 64 位截断指针，folder picker
已经踩过一次。

安全规则：
- 只碰**我们自己起的**进程的窗口（pid 来自那次 Popen）
- 全程 best-effort，失败就保留标题栏，功能不受影响

**3. 最小化端点**

网页也无法最小化 OS 窗口。`POST /client/window/minimize` 用
`ShowWindow(hwnd, SW_MINIMIZE)`。只在接管到原生窗口时显示按钮
（`data.native_window`），Firefox 标签页模式不显示 —— 那里既没有自己的
窗体头，最小化也没意义。

### 实测

- 启动日志出现「已隐藏系统标题栏」
- 截图确认顶部无 Windows 蓝条，第一行就是身份栏 + 三个自绘按钮
- 自己头像 40px、好友行 34px、行高 46px、最小化按钮可见
- 针对性回归 **121 passed**（app/run/launcher/directory）

---

## 外层窗体残留的真正根因（2026-10-23）

上一版加了 `strip_frame` 并打印"已隐藏系统标题栏"，但用户看到的**还是有个
外层窗体**。查完发现是三层叠加的问题。

### 根因一：清 style 不够，窗口矩形永远比客户区大

清掉 `WS_CAPTION | WS_THICKFRAME | ...` 之后，`GetWindowLong` 确实显示
`CAPTION=False`，看起来成功了。但实测：

```
window=(1537,1032)  client=(1521,1024)  差=(14,7)
```

窗口矩形比客户区大 14×7 —— 那是 Windows 11 DWM 画的圆角和阴影，它就长在
窗口矩形和客户区之间那一圈里。**样式清得再干净，这一圈还在。**

修法：加 `WS_POPUP`（`0x80000000`，注意 Python 里要写成 `-0x80000000`，
`SetWindowLongW` 的参数是 32 位有符号 LONG，传正数会溢出）。
但加了 POPUP 差值仍是 14×7 —— 这一圈是 Chromium 自己算的，改窗口尺寸没用。

### 根因二：`find_window_for_pid` 可能选中隐藏的辅助窗口

Chromium 同时有 `Chrome_WidgetWin_0`（隐藏辅助窗口）和 `Chrome_WidgetWin_1`
（真正的顶层窗口）。只按 pid + 可见性筛选，可能改到一个谁也看不见的窗口，
然后以为成功了。加类名过滤。

### 根因三（决定性）：最大化

试过把窗口挪到 (0,0) 并按客户区尺寸重设，无效。可靠的做法是**最大化**：

```
最大化后 位置:(-7,-7) 尺寸:2574x1406  client:2560x1392
```

窗口矩形超出屏幕 7px，但那 7px 是**不可见的阴影**，本来就看不见；客户区
正好铺满屏幕。视觉上就是纯净的无边框。

### 顺带修掉

- 编辑时误删了 `prepare()`，导致日志不再出现"已隐藏系统标题栏"（启动看着
  正常，实际什么也没做）。补回。

### 实测

- 启动日志：「已隐藏系统标题栏」
- 窗口：位置 (-7,-7)、尺寸 2574×1406、`IsZoomed=True`
- 针对性回归 109 passed

---

## 顶部横栏排查结论（2026-10-23）

用户连续报"左上角还有 markovCryptor 客户端的横栏"。查下来是**我自己的
排查方法有盲区**，教训比结论值钱。

### 我错在哪

我一直用 `GetWindowLong(GWL_STYLE)` 判断"有没有标题栏"，看到
`CAPTION=False / POPUP=True / IsZoomed=True` 就说"已经好了"，
**并用这个结论去否定用户亲眼看到的截图**。

实际上用户看到的横栏有两种可能，我只查了第一种：

| 来源 | 特征 | 能否用 ctypes 去除 |
| --- | --- | --- |
| Windows 非客户区标题栏 | `WS_CAPTION` 置位 | 能 |
| Chromium 在客户区里画的窗口装饰 | 样式查不到 | 不能 |

第二种根本不是窗口样式问题，查 `GetWindowLong` 永远查不出来。

### 真正的结论（已验证）

- **`--app` + ctypes 去边框 + 最大化** 是可用组合：实测窗口
  `(-7,-7) / 2574x1406 / IsZoomed=True / POPUP=True / CAPTION=False`，
  顶栏消失
- **`--kiosk` 不可用**：确实不画任何装饰，但独占全屏、吃掉 Esc，而且实测
  在已有 Chrome 实例时直接退出、连窗口都不开
- `--disable-features=WebAppWindowControlsOverlay` **无效**，不能去顶栏

### 误删函数的事故

清理常量时用 `s[start:end]` 删除区间，把 `_is_chromium` /
`find_default_browser` / `_read_default_browser` 一起删掉了。测试立刻抓到
（`NameError: name '_is_chromium' is not defined`），从 git 恢复。

**教训：改单个常量不要用"从 X 到 Y 整段替换"**，区间端点很容易跨过别的
函数。用精确的 old/new 文本替换。

### 流程教训

用户给了截图说"还有"，我不该用"我查的样式是对的"去反驳。
**用户的观察是事实，我的检测方法才有可能是错的。**

---

## 标题栏带账号信息 + 精简窗口按钮（2026-10-23）

用户要求（原文）：「窗口原先的标题栏 cryptor 什么的，改为
"KC - 用户昵称（qq号）"这个格式，把用户登录的账户信息更新在这个默认标题栏
即可，并且去除你在界面内右上角的退出等按钮，只留下一个配置按钮，并且图标
放大。」

改动（全在 `index.html`）：
- `<title>` 占位改为 "KC"；`applyState()` 里按
  `KC - {昵称}（{QQ号}）` 更新，未登录时只有 "KC"（避免出现
  "KC - undefined（null）"）
- 删掉 `#btnMin`/`#btnQuit` 两个按钮及其绑定（`minimizeWindow`/`quitApp`
  里的最小化调用一并移除）；`/client/window/minimize` 端点保留但不再有
  前端调用方
- `.winbtns .iconbtn` 从 32×28 / 14px 放大到 40×34 / 20px

实测（Playwright）：`窗口标题: 'KC - 术力口宝贝（1741473279）'`、
设置按钮 40×34、退出/最小化计数 0、无 JS 错误；截图确认横栏文字正确。
针对性回归 109 passed。

---

## 最近头像 + 输入栏丢失 + 标签计数 + 齿轮图标（2026-10-23）

用户报四项：最近栏头像没同步；输入栏不见了；最近/好友要带总数；设置按钮
换成大号 SVG 齿轮。

### 输入栏丢失 —— DOM 结构被之前的一次修复弄坏了

排查链（值得记下来，因为每一环都像"正确的修复"）：

1. 最初以为 `.workspace` 缺 `min-height:0`，加上 —— 无效
2. 以为 `#chat` 需要 `height:100%`，加上 —— 反而多出 82px
3. 以为缺 `grid-template-rows:1fr`，加上 —— `#chat` 塌成 0
4. 以为 body 缺 `height:100vh`，加上 —— body 行高对了（82+818），
   但 `.workspace` 仍是 900
5. **根因**：DOM 检查发现 `.workspace` 有 **3 个**子元素 ——
   `aside`、`#modal`、`#chat`。`#modal` 是之前修 `</main>` 位置时被错放进
   来的，它占了 grid 第 2 行，把 `#chat` 挤到 `auto` 行
6. 移出 `#modal` 时又把 `#empty` 一起动了，结果 `#empty` 没闭合、
   `#chat` 被包进了 `#empty` 里 —— `chat.parentElement.id === "empty"`
7. 补上闭合，再把 `#empty`（原本裹在一个多余的 `<main>` 里）移进
   `.workspace` 与 `#chat` 同格互斥

最终布局约束（全部必要，缺一个就崩）：

```css
body { height: 100vh; grid-template-rows: auto minmax(0, 1fr); }
.workspace { display:grid; grid-template-columns:280px 1fr;
             grid-template-rows:1fr; min-height:0; overflow:hidden;
             height:100%; }
#chat, .empty { grid-column:2; grid-row:1; }   /* 同格互斥 */
#chat { display:flex; flex-direction:column; min-height:0; overflow:hidden; }
```

教训：**布局问题要先用 `getComputedStyle` 把祖先链的高度和 display 打出来**，
不要凭"缺哪个属性"猜。这次猜了 4 次，每次看起来都合理，全错。

### 最近栏头像

后端 `/client/state` 的 `conversations[]` 没有 `avatar` 字段（好友列表有）。
加 `_peer_avatar(account, peer)`：优先好友记录里缓存的 URL，否则按号码现拼
（陌生人也能发消息，他们的行也需要头像）。

### 标签计数

state 加 `counts: {conversations, friends}`；标签页渲染成
`最近 (1) / 好友 (29)`，小号、淡色、tabular-nums。

### 设置按钮

文本 `⚙` 换成内联 SVG 齿轮（24×24 viewBox，描边风格，跟随 currentColor），
尺寸从 32×28/14px 改为 **40×40**，与头像同尺寸。

### 实测

四项全过：最近列表头像 1/1、输入栏完整可见（y=822，高 46）、
`最近 (1) / 好友 (29)`、齿轮 SVG 40×40；页面无滚动（900=900）、无 JS 错误。
针对性回归 98 passed。

---

## 重构阶段 A：拆分 CSS + core 模块（2026-10-23）

用户判断："打包时间长一点对开发效率的影响，远低于代码堆在一起难以查改"，
决定做一次彻底重构。规划见本节末尾的完整阶段表。

### 目标架构

前端从 1 个文件拆成「结构 + 5 个 CSS + 12 个 JS 模块」：

```
webui/
  index.html        只剩结构，无内联 CSS/JS
  css/              tokens / base / layout / components / overlays
  js/core/          dom / api / store / format
  js/views/         idbar / list / chat / composer / covert      （阶段 B）
  js/panels/        settings / overlays                          （阶段 C）
  js/app.js + boot.js                                            （阶段 C）
```

**不使用打包器**：`<script type="module">` 和 `<link>` 都是浏览器原生能力。
代价是首屏多几个请求 —— 全是本地回环，实测无感。换来改完刷新即生效、
无 node_modules、打包只需拷文件。

### A-1：拆 CSS（已完成）

按顶层规则切分成 70 个原子块，按选择器特征归入 5 个文件，**纯搬移不改写**。

验证：5 个样式表全部加载、零 4xx、`idbar` 高 82、输入栏 y=822、
页面无滚动（900=900）—— 与拆分前逐项一致。

### A-2：抽 core 模块（已完成）

`js/core/` 四个模块都是叶子依赖（不引用任何视图代码），所以最先搬：

| 模块 | 内容 |
| --- | --- |
| `store.js` | 唯一状态 `S`（刻意保持很浅，不做 Redux 式数据流） |
| `dom.js` | `$()` / `toast()` |
| `api.js` | `api()` + `REQUEST_TIMEOUT` |
| `format.js` | `fmtTime()` / `avatarFor()` / `setAvatar()` |

搬完立刻发现一处漏搬：`api()` 依赖的 `REQUEST_TIMEOUT` 常量留在了
index.html，运行时表现为 `REQUEST_TIMEOUT is not defined`，但**页面不报错**
—— 只是每个请求都失败，界面停在空状态。靠 `.toast` 元素的文字才定位到。

### 测试同步更新（`tests/test_client_webui.py`）

原 `ScriptTest` 只认内联 `<script>`，拆分后失效。改写为两组：

新增 `AssetsTest`：
- 5 个样式表按固定顺序 link（后面覆盖前面，乱序就是配色错乱）
- 样式文件确实存在
- `index.html` 里不许再有内联 `<style>`
- 脚本必须是 `type="module"`

扩展 `ScriptTest`（跨文件）：
- core 四个模块存在
- **相对 import 全部能解析** —— 浏览器里 import 失败是静默的，页面停在
  半初始化状态，看起来像"数据没加载"
- `$("...")` 引用的 id 都真实存在
- **同一顶层符号不在两个文件里各定义一次** —— 拆分的经典错误是搬了新的
  忘了删旧的，两份实现，改一份不生效，且毫无报错

两条关键测试都注入 bug 验证过能 FAIL（改坏 import 路径 / 把 `S` 定义两次）。

### 后续阶段

| 阶段 | 内容 | 状态 |
| --- | --- | --- |
| A | CSS + core 模块 | ✅ |
| B | views/（idbar→list→overlays→chat→composer→covert） | 待做 |
| C | panels/ + app.js + boot.js，删除内联 JS | 待做 |
| D | 后端：抽 services/，app.py 拆 4 个 router | 待做 |
| E | 结构测试扩展到新布局、全量回归、打包 | 待做 |

后端现状：`app.py` 1073 行，其中 `create_client_app()` 是 **665 行的单个
函数**，25 个端点全定义在它的闭包里 —— 这是阶段 D 的主要目标。

## 重构阶段 B：视图层抽取（2026-10-23）

按 `idbar → list → overlays → chat → composer` 顺序搬，每搬一个立刻浏览器验证。

| 模块 | 行数 | 内容 |
| --- | --- | --- |
| `views/idbar.js` | 34 | 头像/昵称/QQ号/在线点 + 窗口标题 |
| `views/list.js` | 113 | 最近/好友列表、标签计数、setMode |
| `views/overlays.js` | 154 | 右键菜单、看大图、F5 屏蔽、`install()` |
| `views/chat.js` | 215 | 消息气泡、历史前插、openConversation |
| `views/composer.js` | 228 | 明文附件、队列缩略图、输入框自适应 |

`index.html` 从 2590 行降到 **1312 行**。

### 关键设计：视图之间不互相 import

`list` 需要「点开一行」的能力，而那个能力属于 `chat`。直接 import 会形成
循环引用 —— **ES module 的循环引用不报错**，只在运行时拿到 `undefined`，
非常难查。

所以统一用**注入**：

```js
List.setOpener(Chat.openConversation);
Chat.setHooks({ onOpen, setState });
Composer.setRefresh(() => refreshState());
```

装配代码集中在 `index.html` 末尾的「装配」段。这条规则应该一直保持。

`COVERT` 状态对象从 covert 视图移到了 `core/store.js` —— chat 也要读它
（判断能不能发、要不要提示信道未建立），放在视图里会制造反向依赖。

### 踩到的坑（都是"静默失败"）

1. **`api()` 的 `REQUEST_TIMEOUT` 常量没跟着搬**，页面不报错，只是每个请求
   都失败，界面停在空状态。靠 `.toast` 的文字才定位。
2. **`ATTACH` 在 composer.js 里被定义两次**（我加的 header + 区块自带），
   报 `Identifier 'ATTACH' has already been declared`。
3. **`cut()` 的结束边界用了 `</script>`**，把中间的启动段整段删掉了 ——
   表现是"没有好友数据"，因为 `refreshState()` 再也不被调用。
4. **启动段被整段复制进了 composer.js**，导致 `List is not defined` ——
   composer 引用了它不该知道的符号。
5. **`idbar.js` 和 `list.js` 都 export 了 `render`**，被新测试
   `test_no_redeclared_top_level_symbols` 抓到，改名 `renderSelf` / `renderRows`。

第 5 条说明阶段 A 加的测试确实在干活。

### 排查方法

浏览器里 ES module 加载失败**不报细节**。有效的定位手段：

```js
await import('./js/views/composer.js')   // 在页面上下文里逐个试，
                                         // 能拿到真正的错误消息
```

比看 `pageerror` 有用得多 —— 后者只会说"某某 is not defined"。

## 重构阶段 C：settings + app.js + template 化（2026-10-23）

### C-1 应用层拆分

| 文件 | 行数 | 内容 |
| --- | --- | --- |
| `panels/settings.js` | 383 | 设置面板、模型管理、模型拖入、全部面板交互 |
| `app.js` | 489 | 状态刷新、发送动作、空状态、分栏、存活探测 |
| `index.html` | **312** | 只剩结构与启动顺序 |

`index.html` 从最初的 **2590 行降到 312 行**。

有一处反向依赖需要说明：`settings.js` 里训练/导入模型后要刷新状态，
于是它 `import { refreshState } from "../app.js"`。这是「面板 → 应用层」
单向依赖，不算循环（`app.js` 也 import settings，但只在运行时调用
`installSettings()`，不在模块求值期用它的导出）。

### C-2 template 化

把三处手工 `createElement` 拼装改成 `<template>` + `cloneNode`：

| 模板 | 用在哪 | 收拢了什么 |
| --- | --- | --- |
| `tpl-msg` | `chat.bubbleFor` | 60 行 createElement → 30 行 |
| `tpl-row` | `list.rowFor` | 分支删 `sub`/`mark`/`badge` 而不是逐个建 |
| `tpl-qthumb` | `composer.renderQueue` | 缩略图 + 删除按钮 |

新增 `core/dom.js` 的 `clone(templateId)`：找不到模板时 **console.error 并
返回 null**，不静默返回 undefined —— 后者会让调用方在后面某行莫名崩掉。

模板放在 `body` 末尾，不参与渲染，因此不影响 grid 布局。

### 测试

`test_body_has_exactly_three_children` 因新增 template 而失败 —— 但这正是
测试该做的事。改成**排除 `<template>` 再数 3 个布局元素**，而不是把期望
数字改成 6：后者以后每加一个模板都要改测试，防线就废了。

新增 `test_templates_exist`（注入改名验证过能 FAIL）。

15 passed, 26 subtests。

## 重构阶段 D：后端拆分（2026-10-23）

### D-1 services/（无状态逻辑）

| 文件 | 行数 | 内容 |
| --- | --- | --- |
| `services/picker.py` | 158 | Win32 文件/文件夹选择器 |
| `services/history.py` | 86 | 历史合并 + 图片后台下载 |
| `services/media.py` | 83 | data URL、缩略图、头像地址 |

### D-2 api/（HTTP 层）

`app.py` 里那个 **665 行的 `create_client_app()`** 拆成 5 个 `APIRouter`：

| 文件 | 端点 | 内容 |
| --- | --- | --- |
| `api/session.py` | 4 | ping / SSE / 最小化 / 退出 |
| `api/messages.py` | 6 | state / 会话详情 / 发送 / 历史 |
| `api/media.py` | 3 | 待发队列 |
| `api/models.py` | 8 | 模型增删改查训练导入导出 |
| `api/settings.py` | 4 | 设置读写 + 文件选择 |

`app.py` 从 **1073 行降到 69 行**，只剩 `create_client_app()` 建 app、
`include_router()` 五次、mount 静态目录。

**关键改造：闭包 → `app.state` + `Depends`**

端点原来闭包捕获 `account` / `watchdog` / `on_quit`，这是"一个函数装下
整个应用"的根源。改成：

```python
def get_account(request: Request) -> Account:
    return request.app.state.account

@router.get("/client/state")
def state(request: Request, account: Account = Depends(get_account)):
```

注意参数顺序：**有默认值的 `Depends` 参数必须排在 `request` 之后**，否则是
`SyntaxError: parameter without a default follows parameter with a default`。

### 迁移踩的坑

1. **相对 import 层级全部要加一层**：文件从 `client/app.py` 搬到
   `client/services/` 或 `client/api/` 后，`from .conversations` 变成
   `from ..conversations`，`from ..model` 变成 `from ...model`。
   最隐蔽的是 `api/models.py` 里 `from .models import ModelLibrary` ——
   搬完后它指向**自己**而不是 `client/models.py`。
2. **`_http_error` / `_entry_payload` / `_data_url` 是三组共用辅助**，
   跟着端点走会重复定义。抽到 `api/common.py`。
3. **正则给端点签名注入 `Depends` 时误伤了嵌套函数** ——
   `quit_client` 里的 `def run_quit(account=...)` 也被加了依赖，
   那不是端点，FastAPI 根本不会处理它。

### 测试辅助

新增 `tests/webui_files.py`：`ui_text()` / `ui_scripts()` / `ui_stylesheets()`。

原因：一批测试在断言"某条 CSS 规则存在""JS 语法正确"，它们原本直接读
`index.html`。拆成多文件后全部失效。这些测试**不该关心文本在哪个文件里**，
只该关心"它被发出去了"。`ui_scripts()` 保留文件边界，因为语法检查必须
逐文件做 —— 拼起来会掩盖单文件错误。

### 全量回归

**698 passed, 70 subtests**（默认跳过 2 个 slow）。端到端确认：标题、
计数、29 个好友带头像、输入栏 46px、设置面板可开关、页面无滚动、零失败请求。

## 修复：点开好友报 refreshState is not defined（2026-10-23）

用户报：「点具体好友提示 refreshState not defined」。

### 根因

视图层改造后，`chat.js` 和 `covert.js` 里调用了 `refreshState()`，但**没有
import 它** —— 只有 `settings.js` import 了。

为什么页面加载时不报：那条路径只有「点具体好友」才走到
（`openConversation` 拉完历史要刷新全局状态）。所以它躲过了所有浏览器
冒烟验证 —— 我之前的验证都只点击到「列表渲染」为止。

### 修法：继续用注入，不 import app.js

`chat.js` / `covert.js` 不能 `import { refreshState } from "../app.js"` ——
`app.js` 已经 import 了它们，那会形成循环引用。而 **ES module 的循环引用
不报错**，只在运行时拿到 `undefined`，比现在还难查。

沿用已有的注入机制：

```js
// chat.js 已有 hooks，加一项
let hooks = { onOpen, afterState, refreshState: async () => {} };

// covert.js 新增
export function setCovertHooks(next) { hooks = Object.assign(hooks, next); }

// index.html 装配
Chat.setHooks({ ..., refreshState: () => refreshState() });
Covert.setCovertHooks({ refreshState: () => refreshState() });
```

### 新增静态检查：`UndefinedSymbolTest`

这类 bug **不会**在页面加载时暴露，只能靠静态检查。新测试解析每个模块，
把「被当作函数调用的标识符」和「本文件声明 ∪ import ∪ 局部定义 ∪
浏览器全局」比对，找出无来源的调用。

写这个测试时踩了一个坑：`_strip_noncode()` 里先剥 `//` 注释，会把正则
字面量 `/\.json$/i.test(...)` 的斜杠当成注释开头，**连同同一行的
`const isModelFile = ...` 一起删掉**，于是那个名字"消失"，报假阳性。

正确顺序：正则字面量 → 字符串 → 注释。判据是「斜杠前面不是标识符或
右括号」才算正则，这样 `a / b` 除法不会误伤。

另有一坑：heredoc 写 `\b`（词边界）会被 shell 当成退格符写进文件，
表现为正则永远匹配不到。改用独立脚本文件 + `chr(92)` 拼接解决。

### 验证

注入回原 bug（把 `hooks.refreshState()` 改回 `refreshState()`），测试
精确报出 `'js/views/chat.js: refreshState()'`。端到端确认：点开好友后零
JS 错误、零 toast、输入栏正常。

**699 passed, 70 subtests**。

## 布局改版：身份栏并入左栏（2026-10-23）

用户要求：「界面应当把设置按钮也合并到左侧，放置在已登录 qq 信息的旁边。
需要控制用户昵称不要显示太长，过长用省略号，右侧留下一个按钮位置。
控制界面放在左侧，右边是两个贯通的消息界面。」

### 改动

**结构**：`header.idbar` 从 body 的直接子元素移进 `aside` 顶部 ——
原来它是整宽横条，把右侧内容区从顶部切断；现在身份栏与列表同宽（280px），
右边两个消息面板从 y=0 贯通到底。

body 的 grid 从 `auto 1fr` 两行变成单行，`.workspace` 自己分两列。

**省略号**：昵称 `.idbar .who` 加 `min-width:0; overflow:hidden`，`.me` 同理。
关键点是 **flex 项默认 `min-width:auto`** —— 不显式置 0 的话内容会把容器
撑开而不是收缩，`text-overflow: ellipsis` 永远不触发。`.winbtns` 加
`flex: 0 0 auto`，昵称再长按钮也占住最后 40px。

**CSS 去重**：列宽 `280px 1fr` 曾在 base.css 和 layout.css 各写一份，
改版时容易漏改一处。收敛到 `.workspace` 一处。

### 测试更新

`test_body_layout_children`：body 从 3 个布局元素变 2 个（workspace + modal）。
新增两条守住这次改动的结构约束：

- `test_idbar_lives_inside_aside` —— 身份栏必须是 aside 的直接子元素，
  放回 body 就会重新变成整宽横条
- `test_settings_button_is_in_idbar` —— 设置按钮必须在身份栏里

### 实测

- 身份栏 (0,0,279,86)，aside (0,0,280,900)，右侧内容 (280,0,1120,900)
- 长昵称截断为「这是一个特别特...」，省略号生效，按钮位置不动
- 页面无滚动（900=900），零 JS 错误
- 18 passed（webui）+ launcher 全过

## 并发重构：发送/接收任务化（第一批，2026-10-23）

用户要求：**按支持双工高并发的情况重构**。每私聊单独限制（发送中禁用按钮
+转圈+可取消）、底部状态栏实时显示总握手/文本排队/图片收排队/发排队、
文本走单进程。

### 性能实测（这是所有设计决定的依据）

| 操作 | 耗时 | GIL |
| --- | --- | --- |
| Markov 文本编码（任意长度） | 0.6–0.75s | **几乎不释放**（纯 Python 循环） |
| 文本解码 | 0.15s | 同上 |
| 图片嵌入 256×256 | 0.16s | 释放（numpy/PIL） |
| 图片嵌入 1600×1600 | **3.26s** | 释放 |

### 原 problem

`async def send` 里同步调加密 → **阻塞事件循环**：发一张大图，所有轮询、
其他好友的消息、握手全部停 3.3 秒。接收侧同样：`deliver()` 跑在读线程上
（注释里写着「维持握手和密文流动的那根线，不能被卡住」，但没有任何保护），
对方连发三张 1600 大图就卡十秒。

### `client/tasks.py`（新增）

发送任务的调度器，两条执行通道按 GIL 特性分开：

- **文本：单线程串行队列** —— 编码不释放 GIL，多线程只会互相抢，排队反而
  更可预期（800 字 0.7s，排 3 条 2.1s，有进度提示可接受）
- **图片：线程池 2 路** —— numpy/PIL 释放 GIL，真并发

规则：

- **同一 peer 同时只有一个在途任务**：消息顺序不能靠猜；已存在则返回
  `None`，端点转 409
- **取消是协作式的**：置 `cancel_event`，`work` 在检查点看它。已编码未发出
  的图**归还**（reserved 记录），不消耗用户图库
- **接收入闸 `inbound_admit()`**：积压上限 20 条，超出直接丢弃并计数 ——
  这是防「对方高强度发 1600 大图」的闸门，不解码、不排队、让用户看见
  「已跳过 N 条」

### 接收侧：terminal 层注入闸门

`Terminal` 不该知道队列策略，所以只加了两个钩子：

```python
self.admit = None     # 返回 False 就跳过这张图的解码
self.release = None   # 每处理完一张调一次
```

`Conversations` 建 terminal 时从 `TaskManager` 接上。跳过的图**不解码、
不显示**，由上层提示。

### API 变化

| 端点 | 变化 |
| --- | --- |
| `POST /client/send` | 校验同步做（422 立刻能报）；真正编码提交后台，返回 `task_id`。同 peer 在途 → **409**。握手仍是同步（轻量控制报文） |
| `GET /client/task/{id}` | 轮询任务状态 + 队列统计 |
| `POST /client/task/{id}/cancel` | 请求取消（协作式） |
| `GET /client/state` | 新增 `tasks` 字段（状态栏数据） |

**错误码保留**：任务跑在后台线程里没法抛 HTTPException，所以 `work` 把
`StegoError.http_status` 抄进 `task.error_status`，前端/测试才能分清
「图太小(413)」「格式不支持(415)」「没握手(409)」「我们崩了(502)」。
`hints`（"换一张更大的图片"）也拼进错误文本。

### 前端

- **`views/sendqueue.js`**（新增）：提交/轮询/取消的统一封装。
  发送中把按钮文案改写为「正在编码…」，**再点就是取消**；
  同一会话在途时另一侧按钮同样禁用
- **`views/statusbar.js`**（新增）+ `#statusbar`：常驻显示
  `握手 n / 文本 排队 n / 图片 排队 n / 收 n`，丢弃计数增长时警示
- 发送前**先清空输入框**：内容已交给后台。失败也不还原 —— 还原会让人
  误以为"没发出去还能改"，而任务可能已在上传

### 测试

`tests/test_client_tasks.py`（新增 14 项）：per-peer 串行、跨 peer 并行、
文本严格按序、取消（排队中不执行/运行中结果作废/未知 id 无害）、
接收闸门（上限内接纳/超出丢弃/消费后释放/计数可清零）、异常后会话解锁、
计数不为负。关键两条**注入验证过能 FAIL**（去掉串行检查 / 去掉闸门）。

`test_client_app.py` 增加 `send()` 辅助（提交+等任务+伪装成响应对象），
原有 409/502/413/415 断言全部保留可用。

**全量 714 passed + webui 18 passed。**

## 尺寸钳制 + 容量估算（第二批开头，2026-10-23）

用户拍板：**放大不超过 1.5 倍**（否则糊得过分）、**超大图先缩小**（缩小安全）、
**只要 ≥256 的图都可以，9 张一起发没问题**、超 9 张才拒绝。
不做发送限流（发送中按钮本来就不能用）。

### 尺寸钳制（`image_stego.py`）

新增三段 + 一个统一入口：

| 常量 / 函数 | 作用 |
| --- | --- |
| `MAX_UPSCALE_RATIO = 1.5` | 放大上限（按**边长**） |
| `SAFE_MAX_SIDE = 1600` | 超过就缩小 |
| `ABSOLUTE_MIN_SIDE = 76` | 放大 1.5× 也装不下握手帧的下限 |
| `fit_within()` | 只缩不放 |
| `normalize_cover()` | **先缩后放**，返回 `(图, 原尺寸, 终尺寸, 说明)` |

**为什么必须"先缩后放"**：缩小是安全的（隐写在缩放之后嵌入），而"放大了
再缩"等于白放。更关键的是——一旦超过 QQ 的尺寸阈值，QQ 会自己缩一次，
那时隐写已经嵌好了，**缩放直接毁掉载荷**。

**为什么是 1.5 倍**：原来 `suggest_size` 用整数倍试探，256×256 需要 364
就够却被放到 768 —— 三倍边长。实测对比：

| 原图 | 需 2400 字节 | 旧行为 | 新行为 |
| --- | --- | --- | --- |
| 256×256 | 5272 块 | 768×768（3×，很糊） | 不放大（超限，报容量不足）|
| 512×512 | 5272 块 | 1024×1024（2×） | **585×585（1.14×）** |
| 2560×1440 | — | 原样（超 QQ 阈值） | **1600×900** |

`channels/image.py` 的 `embed()` 改走 `normalize_cover()`，并把尺寸变化说明
放进 `used_options["resize_note"]`，前端能显示"从 A 缩到 B"。

### 容量估算（`services/estimate.py` + `POST /client/estimate`）

**逐张按待选顺序累加**，不用平均容量——队列里可能混着 256×256 和
1600×1600，平均值毫无意义。

返回：`{ok, images_needed, image_limit, queued, repeated, items[], reason}`，
`items[]` 每项带 `slot/size/capacity/takes/scale/fits`。

**不足 9 张时循环填充**：同一张源图可以反复当载体（每次嵌入不同内容，
产生的图片互不相同），这样 3 张图也能发满 9 个槽位，`repeated` 会标明
有多少槽位在重复用图。

实测（`data/` 里 9 张 512 左右的图）：

```
   4 字 → 1/9 张
 300 字 → 1/9 张
1000 字 → 3/9 张
3000 字 → 6/9 张
8000 字 → 9 张不够：最多装 14030 字节，还差 9970 字节
```

`QueuedImage` 新增 `size` 字段（`_measure()` 同时返回容量和像素尺寸），
否则"这张 512×512 能装 408 字"根本显示不出来。

### 测试

`tests/test_client_estimate.py`（新增 13 项）：空队列、短文本占 1 张、
占用随文本单调增长、永不超过 9 张、9 张不够要拒绝、不足 9 张要循环、
**显示容量必须等于真实容量函数算出的值**（对不上就说明界面在骗人）、
超大图标明缩小、过小图跳过、文本信道上限/边界/与队列无关。

改写两条旧几何测试 —— 它们断言的是「整数倍放大」这个已废弃行为：
`test_small_covers_are_upscaled` 拆成「会放大且在 1.5× 内」+
「放大也不够就保持原样」；GIF 那条的期望文案从"太小"放宽为
"容量不足 / 太小"（新文案更具体：最多 0 字节、需要 32 字节）。

**全量 732 passed。**

## 前端容量条（第二批，2026-10-23）

用户确认的操作取舍：**保留**点 × 排除、导入自动插队首、预览接下来 9 张；
**砍掉**拖拽排序（容量只看总和，顺序不影响）和手动重随机（被"预览"取代）。

### `views/capacity.js`（新增）

输入框下方常驻一条容量条，300ms 防抖调 `/client/estimate`：

```
将会发送 5/9 张   密文 15000 字节 · 候选 9 张        ← 图片信道
1 234 / 2 000 字符                                    ← 文本信道
```

装不下时：数字变红 + 警告文案（"还差 N 字节"）+ **发送按钮禁用**。
用户边打字边知道结果，而不是点了发送白等一次编码再收 413。

### 两个实测踩到的坑

1. **异步返回乱序**：连打几个字会并发多个估算请求，先发的可能后回来，
   旧结果覆盖新结果 → 按钮抽搐（实测 300 字被禁用、1000 字反而能点）。
   修复：请求带序号，只认最后一次。
2. **与遮挡层互相覆盖**：`updateCovertControls()` 在未握手时禁用按钮，
   容量条回调又按容量解锁 —— 两边抢同一个属性。修复：容量回调在
   `locked/waiting` 状态下不碰按钮（此时按钮的禁用不归它管）。

### 验证

`/d/_Temp/verify_capacity.py`：起真实双账号 + InProcessOneBot + 真握手 +
Playwright 输入，断言按钮禁用状态：

```
   4 字 → 将会发送 1/9 张   可发送
 300 字 → 将会发送 1/9 张   可发送
1000 字 → 将会发送 2/9 张   可发送
3000 字 → 将会发送 5/9 张   可发送
8000 字 → 将会发送 9/9 张   禁用："最多装 16735 字节，还差 7265 字节"
```

`test_no_redeclared_top_level_symbols` 抓到 `current` 在 capacity.js 和
sendqueue.js 重复定义 → capacity 侧改名 `latestEstimate`。这条防线再次
证明了自己的价值。

**全量 732 passed。**

## 修复：设置界面「浏览…」按钮点了没反应（2026-10-23）

用户反馈设置页两个选择文件夹按钮无反应，怀疑是浏览器结构不支持。

**不是浏览器的问题。** 浏览器只是发一个 HTTP 请求，真正弹对话框的是宿主机
上的 Python —— 这在 `--app` 模式下完全可行。

### 根因：ctypes 结构体字段类型错

`services/picker.py` 里 `BROWSEINFOW.pszDisplayName` 声明成了
`wintypes.LPWSTR`（= `c_wchar_p`，不可变），而实际传进去的是
`create_unicode_buffer(260)` 返回的 `c_wchar_Array_260`：

```
TypeError: incompatible types, c_wchar_Array_260 instance
           instead of c_wchar_p instance
```

这个字段是**输出缓冲**（shell 把所选文件夹的显示名写回来），必须是
`ctypes.POINTER(ctypes.c_wchar)`。

异常又被 `pick_folder()` 的兜底 `except` 吞成 `None`，界面于是表现为
「点了没反应」，日志里也什么都没有 —— 这类静默失败最难查。

### 修复

- 字段改成 `POINTER(c_wchar)`，结构体提到模块级（`_browse_info_structure()`）
- 验证：程序化检查窗口句柄，确认 `#32770` 对话框真的出现，标题正确显示中文

### 顺带修掉两个假阳性测试

修好之后跑全量，测试**卡在 45%** —— 因为它们依赖「对话框永远不出现」：

| 测试 | 问题 |
| --- | --- |
| `test_missing_tkinter_does_not_break_settings` | mock 掉 `sys.modules["tkinter"]`，但 picker 早就不用 tkinter 了 → mock 是空操作，测试**真的弹了个对话框**并等到超时 |
| `test_picker_returns_instead_of_blocking` | 真的调 `pick_folder()`，修复前它「通过」恰恰是因为 bug 让它立刻返回 —— 假阳性 |

前者改成注入 `pick_folder_win32` 故障；后者改成 mock 掉真实调用、只验证
**线程语义**（任何线程都能把控制权交回）。对话框本身的正确性交给
`tests/test_client_picker.py`。

### 新增 `tests/test_client_picker.py`（5 项）

- `pszDisplayName` 必须能接住 `create_unicode_buffer`（赋值不抛 TypeError）
- 字段声明类型必须是 `POINTER(c_wchar)`（**注入回 LPWSTR 验证过能 FAIL**）
- 结构体大小 64 字节（64 位，含尾部对齐）
- 字段顺序符合 Win32 定义
- 失败要进日志，不能静默返回 None

**全量 737 passed。**

## 图片输入：拖入 / 点选 / 粘贴（第二批，2026-10-23）

按之前确认的取舍实现：**插队首**（用户刚选的就是这次想用的）、
**点 × 排除**（已有）、**砍掉**拖拽排序和手动重随机。

### 后端

- `PictureQueue.insert_front(raw, name=)` —— 图片插到队首，容量和尺寸当场算好；
  垃圾数据拒绝不进队；**超出上限时挤掉队尾的图库图片**，而不是刚插进来的
  这张（反过来的话用户拖三张、前两张立刻被第三张挤掉，像导入失败）
- `QueuedImage.origin`：`"folder"` / `"upload"`。临时图不写磁盘（零持久化）、
  **不参与轮转记账**（导入过一张不该让图库某张被跳过）
- `POST /client/queue/import`：接受 data URL 数组，部分成功时好的留下、
  坏的报告（不是全部丢弃）
- `queue_items` 增加 `origin/size/capacity` 三个字段

### 前端

- `views/dropper.js`：三条入口统一走 `/client/queue/import`
  - **拖放**：dragenter/leave 计数控制提示层（避免子元素触发闪烁）；
    `dragover` 必须 preventDefault 否则浏览器直接打开文件
  - **粘贴**：绑在 `document` 上，输入框里 Ctrl+V 截图也接得住
  - **点选**：「添加」按钮 + 隐藏的 `<input type=file multiple>`
- 队列缩略图加两个徽标：
  - **容量**（左下）：「560字」「2503字」，太小的红底「太小」
    —— 128×128 和 1600×1600 差几十倍，光看缩略图判断不出来
  - **来源**（右下）：「新」= 用户拖入，蓝色角标

### 测试

`tests/test_client_queue_import.py` 9 项：插队首、带容量尺寸、垃圾拒绝、
origin 标记、过小拒绝、**溢出先挤图库图**、全临时图也有上限、
**导入不动轮转记账**、上限可配置。注入「插队尾」验证过能 FAIL。
`test_client_app.py` 另加 5 项端点测试（含部分成功语义）。

`test_no_redeclared_top_level_symbols` 又抓到两处重复定义
（`setRefresh`/`install`），改名 `setImportedCallback`/`installDropper`。

**实测**（Playwright 真实拖放事件）：拖入 1024×1024 图 → 队首出现
「2503字 + 新」，原队首 560 字的图排第二。

**全量 751 passed。**

## 队列自动同步 + 图库状态（第二批，2026-10-23）

用户要的：**文件夹变了界面自己跟着变**，不要手点「补图」；状态栏显示
文件数、轮转模式、已发总数。

### `queue.sync_with_disk()`

先比对**文件夹指纹**（mtime + 文件数），变了才真扫。理由：每 2.5 秒轮询
都 scandir 几十张图是浪费；而用户几小时不动文件夹时，指纹比对几乎零成本。

mtime 精度在某些文件系统只到秒，所以指纹里还带文件数做补充。

同步做三件事：

1. **移除磁盘上已不存在的图库图片**——否则用户删了图、界面上还在，点了
   发送才失败，还不知道错在哪
2. **清理 `_used` 记账**（`&= live`）——已删文件不该继续占着"已用过"
3. **补新图**

**临时图（`origin="upload"`）不参与移除**：它们没有磁盘对应，清掉等于把
用户的拖入操作静默作废。

### 两个实测抓到的 bug

1. **补图会重复入队**：`_used` 表示「本轮取过」而不是「队列里有」，
   第一次 sync 就把 3 张补成 6 张。修复：`_next_candidate` 加 `skip`
   参数，把队列里已有的文件排除掉，并且本次已选的也加进 `skip`。
2. **加一张补三张全是同一张**：`fresh` 非空时没排除已选的。修复同上。

### 统计字段

`queue.stats()` 新增：

- `total` —— 文件夹里能用的图总数（≠ 队列长度，用户要看出"还有 40 张没轮上"）
- `sent` —— **本次运行**累计发送数，在 `take()` 里加
- `folder_names` —— 配置了哪些文件夹

### 前端

状态栏最左新增：

```
图库 24 张 · 队列 9/9 · 随机 · 已发 0
```

`pick` 翻成中文（random→随机、sequential→顺序）——用户看不出两者区别，
但"下一张是什么"完全取决于它。悬停显示文件夹名和完整说明。

「补图」按钮改名「重新扫描」，title 说明平时自动、这只是手动兜底。

**未握手时队列依然可见可操作**（`queuefloat` z-index 30 > `veil` 20），
这条原来就是对的，本次确认。

### 新增 `test_exported_symbols_really_exist`

实测抓到 `Dropper.install is not a function` —— 上一次改名
（`install` → `installDropper`）漏改了 index.html 的调用处，**浏览器不报
这个**，只在运行时抛 TypeError，页面看着"打开就是空的"。

原有 `test_imports_resolve` 只查 import **路径**是否存在，不查**符号**是否
真的导出。新测试解析每段 module 脚本里的 `Namespace.symbol` 用法，与
模块的 export 列表比对。注入回原 bug 验证过能 FAIL。

**全量 761 passed。**

## 安全加固：握手身份绑定 + TOFU + 手动确认（2026-10-23）

用户问：「拿到算法原文和完整聊天记录，能否据握手包解开双方信息？」
审计结果分三层。

### 审计结论

**密码学原语是可靠的**：X25519 临时密钥、HKDF-SHA256、AES-256-GCM（每条
随机 nonce）、PBKDF2-SHA256 310k 轮。**私钥从不离开本地**，所以光有源码和
聊天记录算不出共享密钥。

**但握手没有身份认证。** `handshake.py` 的 docstring 自己写着 "textbook
unauthenticated DH"，提到 "may append a MAC"——**但代码里从来没实现**。

实测的 MITM 攻击（`HandshakeParty` 三方模拟）：

```
Alice 与 Mallory 建立会话成功: True   ← 因为没有身份认证，Alice 毫无察觉
Alice 发的消息，Mallory 能解密: b'secret message'
```

而**默认 PSK 是硬编码的 `"pbx1"`**，攻击者连猜都不用猜。

### 三层修复

**1. 帧内 MAC（`core/identity.py` + `terminal/protocol.py`）**

握手帧从 `pub(32) | mode` 变成 `pub(32) | mac(16) | mode`，54 字节。
MAC = `HMAC-SHA256(mac_key, DOMAIN || public || role)` 取前 16 字节，
`mac_key = derive(psk, "handshake-mac")` 做域分离。

`_on_handshake` **先验 MAC 再碰会话状态**——未通过验证的公钥绝不进
`absorb()`，否则任何知道协议格式的人都能往会话里塞密钥。

无 MAC 的旧格式帧一律拒绝（它是可以随便伪造的）。

**2. TOFU 指纹（`core/identity.py` 的 `KnownPeers`）**

首次见到某人的公钥就记住指纹（SHA-256 前 8 字节 = 16 个十六进制字符），
之后每次比对。变了就 `changed` 警告。

存在 `%APPDATA%\markovcryptor\peers.json`——**这是除用户配置外唯一持久化的
东西**。理由：TOFU 的全部价值就在"记住上次见到的是谁"，内存里存等于每次
重启都重新信任，中间人只需等你重启。

存的是**指纹**不是密钥，也不含任何消息内容（有测试守着这一条）。

写盘用临时文件 + `os.replace`：中途断电不能把指纹册写成半截 JSON，
那会让所有对端都变成"首次见面"，静默丢掉全部验证历史。

**3. 不自动回复握手（用户明确要求）**

原行为是收到请求立刻取图、嵌入、发出——从收到到回复可能只有 0.2 秒。
用户指出三条代价，全部成立：

- QQ 侧看就是机器行为，而这是个靠"看起来像正常聊天"活着的工具
- 用户失去核对指纹的机会，TOFU 就白做了
- **客户端挂着时，任何知道口令的人都能自动把连接建立起来**

改为：收到请求只挂起（`pending_request`），界面显示「接受并回复 / 忽略」，
用户点了才发。`_auto_respond` 整个方法删除。

### 已验证的端到端行为

`/d/_Temp/verify_manual_handshake.py`（双账号 + 真握手）：

```
Bob 端 待确认: True           Bob 端 已建立: False
Bob 已发出的包数: 0           ← 不自动回复
--- 模拟 Bob 的用户点接受 ---
回复已发出: True
接受后 Bob 已建立: True       接受后 Alice 已建立: True
已记录指纹: 93d3ebe15f17fcfb
```

### 前端

新增 `pending` 遮挡层状态：`🤝 对方请求建立加密连接`，显示对方指纹
（首次 / 一致 / **变了** 三种文案），两个按钮「接受并回复」「忽略」。

指纹变了时用红色大字 + 明确警告「可能是重装了客户端，也可能是有人在中间
……请在别的渠道核对指纹」。

新增端点：`POST /client/handshake/{peer}/accept`、`.../ignore`。
`GET /client/conversation/{peer}` 增加 `handshake` 字段。
`GET /client/state` 的会话项增加 `pending`。

### 测试

`tests/test_identity.py`（新增 18 项）：MAC 往返、错口令拒绝、篡改公钥、
篡改 MAC、帧绑定发送方、长度异常；指纹稳定性/长度/无碰撞；TOFU 首次/
一致/**变更**/重启后仍记得/信任新键/忘记/损坏文件不阻塞启动/只存指纹。

`test_terminal.py` 新增 `HandshakeAuthenticationTest`（4 项）。

**改写 5 个模块里依赖"自动回复"的测试**（共约 30 处）：`_handshake()`
辅助函数里加 `respond_handshake()`，模拟用户点击。

**全量 784 passed。**

## 连接状态栏：密钥年龄 / 等待超时 / 换钥（2026-10-23）

用户的三个问题，逐条定案：

### 1. 超时多久？—— 两段式，不自动放弃

| 时点 | 显示 |
| --- | --- |
| 0–30s | 「已发起握手，等待对方回应」 |
| 30s–10min | 「已等待对方回应 N 秒」+「对方可能暂时不在」 |
| >10min | 「对方可能没收到 —— 建议点右侧重新发起」（黄字加粗） |

**不自动放弃**：对方可能真的晚点回来。把"我在等"变成"可能出问题了"，
决定权在人。

### 2. 密钥主动换新？—— 显示年龄，人决定

用户提了三个方向：主动过期重发 / 附在大包末尾 / **显示年龄让人决定**。
采纳第三个，另两个否决：

- **附在大包末尾**：握手要双向（对方得回），单方面塞在消息尾等于要求
  对方立即响应；大包本来就要 3 秒编码，再叠握手更慢
- **定期自动换钥是反效果的**：换钥要对方确认（握手是人工确认的），
  对方离线就卡在 WAITING，反而把一个能用的通道弄成不可用。且 X25519
  临时密钥已提供前向保密，换钥并不减风险
- **显示年龄才是对的**：密钥年龄是真实的安全信号（会话越长，同一套
  密钥暴露的窗口越大），而何时该换取决于"你现在要发的内容多敏感"，
  只有用户知道

### 3. 强口令？—— 选做，且**不做程序随机**

用户同意选做，并问"程序自己随机会有问题吗"。会，而且是致命的：

- **双方怎么知道同一个口令？** 必须额外通道传递（复制粘贴/口令卡）
- 用户会存哪？大概率截图/记事本 → 泄露
- 忘了无法恢复，只能双双重设
- 换口令后 TOFU 指纹册全变"变更"警告

且**口令从来不是身份**（它是共享的），真正的身份由 TOFU 指纹承担。
所以保持"用户自己设"，首次启动醒目提示即可。

### 实现

**后端**（`session.py`）：`key_since`（密钥建立时刻）、`requested_at`
（发起请求时刻），暴露 `key_age` / `waiting_for` 两个属性。
`_handshake_state` 返回 `key_age/generation/rekey/waiting_seconds/
my_fingerprint`。

**前端**（`views/keybar.js`）：**同一栏**报告所有连接状态 —— 用户的
明确要求（"分两处会让用户以为是两件事"）：

```
🔑 会话已建立 6 秒   密钥 f9e0…429b          [换新密钥]
⏳ 请求换钥，等待对方确认（旧密钥仍可用，已用 5 分）  [重新发起]
```

换钥请求与首次握手文案区分：换钥注明「旧密钥仍可用」，不吓人。
年龄每秒本地累加（不重新请求），30 分钟起变黄（提示色，不是报警）。

### 抓到的测试漏洞

`app.js` 有一处孤立的 `}`，`node --check app.js` **通过** —— node 对
`.js` 按 CommonJS 解析。浏览器里模块加载失败是静默的（只有一行
net::ERR），页面空白，极难定位。

新增 `test_scripts_parse_as_es_modules`：把每个 JS 复制成 `.mjs` 强制
ESM 解析。注入孤立 `}` 验证过能 FAIL。

**实测**（双账号真握手 + Playwright）：

```
未建立 【keyBar 可见】: False
已建立 【keyBar 可见】: True
  keyAge: 会话已建立 6 秒      keyFp: 密钥 f9e0…429b
  2 秒后 keyAge: 会话已建立 10 秒     ← ticker 在走
```

**全量 785 passed。**

## 握手去重 + 单图载荷补满（2026-10-23）

用户两条要求：

> 要考虑对方在你未能回复第一个握手情况下反复发送握手，这种时候点按钮应当
> 只能回复最新一个（并且写着是几秒之前发送的握手），而且对端在收到一串
> 对自己先前排队等候的消息的握手回复时也应当做最合理的自动处置，是否允许
> 一次性建立一堆合理连接？那没有任何必要，只是最新一个即可？

> 图片信道的话我用图片就也有这个意思在里面，他只能知道我发了多少图，而每个
> 图应当是写满最大隐写额度的密文才对

### 一、握手去重与「只回最新」

**收方**：`Terminal._seen_requests`（环形 64 个）记住见过的对端公钥。
X25519 公钥每次握手都是随机的，**重复出现几乎只可能是重放** —— 攻击者把
一张合法握手图反复丢过来，用户会被一堆相同请求淹没，很容易误点。

新请求**取代**旧请求（不排队）：一次性建立一堆连接没有任何必要，而且用户
点「接受」时根本分不清在回哪个。`pending_since` 记录到达时刻，界面显示
「对方请求建立加密连接（12 秒前）」。

**发方**：`Session.absorb` 收到重复 RESPONSE 时直接返回 False，不再派生。

实测抓到的 bug：

```
同一个回复到达 3 次 -> gen=1 -> gen=2 -> gen=3
```

原来每次都 `_derive()`，**刚建立好的会话被瞬间换掉，已经用它加密的消息
就解不开了**。修复后 gen 稳定在 1。

判据是「已建立 + 公钥相同」。真正的新回复（换钥）仍然生效 —— 去重不能把
正常重钥堵死（有测试守着）。

### 二、图片信道单张载荷补满

不补的话，每张图的载荷长度和它承载的明文份额成正比：一张只装 12 字节的
图，和一张装 400 字节的图，从「能装多少、实际用了多少」就看得出来。

现在切分时把每片补到该图的**实际容量**：

```
三张图容量 [441, 712, 258]
密文  36 字节 -> 载荷 [441, 712, 258]   ← 全部满载
密文  90 字节 -> 载荷 [441, 712, 258]   ← 全部满载
```

观察者只能知道「发了几张图」——图片数量本来就暴露了信息量，单张长度不该
再泄露第二份。

**填充用随机字节**：可压缩的数据（全零）会让图片体积异常变小，反而成了
新的特征。

### 一个设计取舍：不靠猜，用独立 kind

分片带填充时需要一个「有效长度」字段告诉接收端哪里是真的内容。试过
「看前 2 字节像不像长度」的启发式，但**正文首字节完全可能落在合法范围**，
那会变成偶发且极难复现的解码错误。

改成独立 kind：`BODY_SLICE="S"`（无填充）、`BODY_SLICE_PADDED="P"`（有填充，
载荷 = 有效长度(2) + 内容 + 随机填充）。显式标记没有歧义。

**踩坑**：`_inspect_image` 只判 `Kind.BODY_SLICE`，新的 padded kind 直接落到
「完整正文」分支被解密，失败后静默丢弃 —— 表现为「消息发出去了但对方收不到，
没有任何报错」。

### 测试

`tests/test_handshake_dedup.py`（新增 14 项）：分片补满/重组正确/padded 标记/
无容量时不补/填充随机/超容量不截断/往返；重放被忽略（**断言 `_seen_requests`
不增长**，只比 `pending` 相等不够——每次重新记一遍时 pending 看起来一样）/
新请求取代旧请求/`pending_age` 增长/重复回复不重建密钥/真新回复仍生效。

两条关键测试注入验证过能 FAIL。

**全量 799 passed。**
