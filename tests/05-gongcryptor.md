## 这是什么

`D:/Projects/GongCryptor/markovCryptor` —— **隐写加密通信系统**。
多算法多信道隐写加解密 + 本地 API 服务 + 批处理 + 新算法可插拔（视频/直播流预留），
提供 WebAPI / CLI / 桌面窗口三种交互。

阶段 0–7 重构、清理阶段、终端协议层、双窗→四窗 UI、QQ(OneBot) 信道接入全部完成。
**290 测试全绿**，最新推送 `2fff8fc`。

## 当前卡点（唯一，进行中）

**修 `.ps1` 缺 UTF-8 BOM**（用户问「目标机 win10 好像没有 ps1？」引出）。

已确认 Win10 自带 PowerShell 5.1（本机 `5.1.26100.9444`），包内 `.ps1` 都是 **NO-BOM CRLF**。

**根因**：PS 5.1 按 ANSI 代码页读无 BOM 的 `.ps1`。
中文注释里的 `号`（UTF-8 尾字节 `\x8f`）在 GBK 下会吞掉行尾 CRLF，
把 `param(` 拼进注释 → 报「`-File` 格式…实际参数…」。

已完成：`tools/make_bundle.py` 的 ps1 写出改为
`b"\xef\xbb\xbf" + body.replace("\n","\r\n").encode("utf-8")`；
加了两个测试 `test_ps1_files_need_bom_and_crlf` / `test_generated_ps1_starts_with_bom`。

**未完成**：`test_generated_ps1_round_trips_through_ansi_decoding` 是残留占位测试，
引用了不存在的 `tools_shim.read_ps1`，**需删除**；然后重跑测试、重新打包、
用 PS 5.1 实测 `.ps1` 可用、提交推送。

## 项目结构 / 命令

- **包名保持 `markovcryptor`**；`data/`、`corpus/`、`models/`、`vendor/`、`dist/`、`build/` 通过 `.gitignore` 排除
- 推送账户 `ttjoint` / `bignesslaski@gmail.com`（`git push` 自动走，**`gh` 不可用**）
- 前端**单文件、零依赖、无构建**：不引用任何外部资源（无 CDN、无字体、无 npm）
- Web API **无 token**（单机本地），端口可配置（`--port` / `MKX1_PORT`），**默认不得用 8000/8080**
- 日志写单行 `key=value`；库对 import 不得写 stderr；终端协议层零 UI 依赖
- **改 `markovcryptor/` 下 Python 后必须重启 `serve` 进程**（uvicorn 未开 `--reload`）；
  只改 `webui/index.html` 则 Ctrl+F5 即可

## 隐写算法要点

- **盲提取**，不依赖原图。判定**只能靠 AES-GCM 认证标签**。
- **多通道载体优先 Y**，非 Y 需超 0.02 才胜出。中心格点 QIM 写 `(k+0.5)*step`。
- **容量判定单一真源**：`blocks_needed` / `max_payload_bytes` / `suggest_size` / `check_cover`。
- `CapacityExceeded` 必须带 `reasons` 与 `hints`；错误提示须保留真实原因（不可笼统说"无法稳定承载"）。
- GIF：像素→粗 QIM step（20 起）→存 GIF→重载验证。默认只嵌首帧。
  `MAX_GIF_UPSCALE=2`、`MAX_GIF_GROWTH=4.0`、`MAX_GIF_UPSCALE_RESULT=160KB`。
- 帧结构：lead + 变长 body；lead 单独 RS 编码且永远只占一块；`nblocks` 用 2 字节。

## 通信语义（用户逐条纠正出来的）

- **不得自造/合成底图**：用户没选图就**拒绝发送**。
- **图片序列是用户的**：不重排、不替换、不跳过、不留未嵌载荷的图。
- **文本混淆的 covertext 就是宿主文本**：作为 `chat_text` 和对端可见，密文窗另显解出内容。
- **明文窗 = 网关视角**：显示网线上真正传出去的内容，不是用户键入的原文。
- **`chat_text` 语义 = "网线内容"**：明文=用户写的字；文本混淆=生成的宿主中文；
  图片混淆=`None`；原始密文只进密文窗。
- 每条消息必须带时间戳；各内容窗口必须自动滚动到最新。
- **握手图不算聊天图片**（`display=None`，只留系统提示，发送方本地记 0 张）。
- **握手回包必须用本端自己的图片**，不用对端发来的图。**没图就等着**（保持 `WAITING`，
  UI 提示，用户加图后自动补发），不自造底图、不静默失败。
- 队列状态在前端（浏览器编辑态），由服务端算 `images_needed`。

## QQ 信道（OneBot / SnowLuma）

- **外接 OneBot v11 over WebSocket，不移植源码、不引入本库**。
  SnowLuma 的 `native/` 平台相关、有独立 EULA、升级频繁 → **不能打进本库分发**。
- **`Terminal`/`session.py`/终端测试一行不改**；`OneBotNetwork` 约 100–150 行。
- **`reportSelfMessage: false` 是硬要求**（否则终端自己跟自己握手）；
  **`messageFormat: "array"` 是硬要求**（图片段要带结构）。
- **每个 UIN 的 OneBot 端口必须不同**，否则第二个号 `EADDRINUSE`。
- 单机双号用**一个** SnowLuma 实例（省一半进程和内存）；跨机器才各起一份。
- 传输层包关联头留在传输层：`packet_id(8B) + index + count`，不塞进加密信封。
  P0 实测三图合并进一条消息 → 包关联头是必需项。
- **所有连 OneBot 的代码统一走 `markovcryptor.onebot_ws.connect()`**，
  禁止手写 `extra_headers=`（websockets 14+ 改名 `additional_headers`）。
- `down` 只结束自己起的 `node … index.mjs`，**不按进程名批量杀、不碰用户 QQ**。
- 自动化边界：下载/配置/启动/WebUI 密码/EULA 都能自动；
  **QQ 登录**不能（SnowLuma hook 已登录进程）；**QQ 号**要先登录一次才知道。

实测通过的账号：**1741473279**（术力口宝贝）、**3925697820**。

### EULA 阻塞
干净 SnowLuma 无 `config/consent.json` 时停在 `awaiting EULA/PRIVACY consent`，
QQ hook 与 OneBot 端口都不起。预置 `consent.json` + `webui.json` 后正常。
`consent.json` 的 `version` 哈希**不是** EULA/PRIVACY 原文 sha256 前 16 位
（`dcb31a7b03a0b64d` ≠ `373c4f7c5d87da11`/`d36474445ce510e2`）；`/api/agreements` 需鉴权（401）。
`run-snowluma.ps1` 识别 EULA 卡住时打印解释并 `exit 2`。

## 打包（`tools/make_bundle.py`，约 600 行）

生成 `dist/qqtest-<版本>-<时间>.zip`（74MB，41 wheel + SnowLuma + install/run/probe 脚本 + CHECKLIST + manifest）。

修过的 6 个真实缺陷（别再犯）：
1. **必须覆盖 cp311/cp312/cp313 三种 wheel**（原来只打 cp311）。
2. **拷 SnowLuma 必须含 `config/` 目录**（否则拒绝启动）。
3. **`install.ps1` 必须装 `markovcryptor[web]`**，不止 `markovcryptor`。
4. **websockets 14+ API 改名**（见上）。
5. **zip 名用 `target.parent / f"{target.name}.zip"`**，
   不能用 `with_suffix(".zip")`（会截掉时间戳）。
6. **`.ps1` 必须 UTF-8 BOM + CRLF**（当前正在修的）。

- `tests/` 无 `__init__.py`，测试内必须用绝对导入 `import fixtures`。
- **测试必须可在干净检出（无 data/corpus/models/vendor）下全绿、零跳过**。
- **冒烟检查必须能 FAIL**。
- 改 `index.html` 用精确锚点或 `edit` 工具，不要大范围字符串替换；改完必须 `node --check`。


---- 
## 最近响应
 已推送 b942b47，302 项测试全绿。

 最终方案：双击 start.bat

 把包拷进虚拟机后，首次运行时执行一次：

 ```powershell
   .\start.bat 1741473279
 ```

 之后直接双击 start.bat 即可，端口和 token 都会自动沿用。

 整个流程耗时 6 秒，输出：

 ```
   === 一键启动 ===
   [1/3] 检查依赖…        已就绪
   [2/3] 准备配置…        使用 onebot_1741473279.json
   [3/3] 启动 SnowLuma…   就绪：OneBot WS 127.0.0.1:9811

   === 就绪 ===
     OneBot WS : 127.0.0.1:9811
     Token     : mkx-1741473279
     WebUI     : http://127.0.0.1:5099/
     发一张图验证：
       .\probe.bat <对端QQ号>
 ```

 然后：

 ```powershell
   .\probe.bat 3925697820
 ```

 就发消息了。不需要打开任何网页。

 三个自动化的实现方式

 都在 SnowLuma 源码里找到的（server-D--Qu9qq.js），不改它一行代码：

 ┌─────────────────────────────────────────────────────┬─────────────────────────────────────┐
 │ 环境变量                                            │ 替代了什么                          │
 ├─────────────────────────────────────────────────────┼─────────────────────────────────────┤
 │ SNOWLUMA_ACCEPT_EULA=1<br>SNOWLUMA_ACCEPT_PRIVACY=1 │ 网页上点同意协议                    │
 ├─────────────────────────────────────────────────────┼─────────────────────────────────────┤
 │ SNOWLUMA_HOOK_AUTOLOAD=1                            │ 在 WebUI 里手动挑 QQ 进程 PID       │
 ├─────────────────────────────────────────────────────┼─────────────────────────────────────┤
 │ SNOWLUMA_WEBUI_BOOTSTRAP_PASSWORD                   │ 「必须大小写+特殊字符」的强制改密码 │
 └─────────────────────────────────────────────────────┴─────────────────────────────────────┘

 密码规则（≥10位、含大小写特殊字符）只在你主动改密码时才会校验；设置引导密码后，mustChangePassword 直接写成 false，完全跳过。

 之前卡住的真实原因

 不是脚本写错，是 Start-Process -NoNewWindow 让 node 继承了调用方的控制台。使用 run-snowluma.ps1 时，PowerShell 会一直等 node 退出，所以 start.ps1 永远不返回。

 改成 -WindowStyle Hidden 后，node 才真正独立运行：

 ┌───────────┬──────────────┬──────────────┐
 │           │ 之前         │ 现在         │
 ├───────────┼──────────────┼──────────────┤
 │ start.ps1 │ 无限挂起     │ 6 秒返回     │
 ├───────────┼──────────────┼──────────────┤
 │ 关窗口    │ 服务跟着退出 │ 服务继续运行 │
 └───────────┴──────────────┴──────────────┘

 这个 bug 你迟早会撞上——双击 start.bat 后窗口卡住不动，看起来像失败，实际上服务已经启动。

 也向你道歉：定位它花了两小时，是因为我跑测试时没加超时，被自己的挂起卡住了。这次全程都用 timeout，并输出到文件。

 唯一不能自动化的一件事

 QQ 必须先登录。SnowLuma 是 hook 已登录的 QQ 进程，不能替你输密码、过验证码。

 ────────────────────────────────────────────────────────────────────────────────

 新包：dist/qqtest-0.3.0-20261020-1705.zip（197 MB）

 如果你之前那份配置（端口 3000/3001）还在，start.bat 会直接沿用，不用重新 qq-init。

