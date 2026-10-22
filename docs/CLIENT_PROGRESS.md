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
