# markovCryptor 发布包逆向与泄漏审计报告

审计对象：`dist/qqtest-0.3.0-20261021-0613/`（0.3.0，2026-10-21 构建）
审计日期：2026-10-23
审计范围：wheel 源码形态、敏感字符串、运行时数据、WebUI 暴露面、协议可逆程度

---

## 结论摘要

**可以发布，但必须先重新构建，并修掉 3 个问题。**

| # | 问题 | 严重性 | 状态 |
| --- | --- | --- | --- |
| 1 | **现有 wheel 是 10-21 的旧版**，不含全部安全加固 | 🔴 阻断 | 必须重建 |
| 2 | **`make_bundle.py` 会把真实 QQ 号和 OneBot access token 打进包** | 🔴 阻断 | 必须修 |
| 3 | **57 个 `.py` 明文源码**，协议完全可读 | 🟡 可接受 | 如实说明 |
| 4 | 默认口令 `pbx1` 硬编码 | 🟡 设计如此 | 已有 UI 警告 |
| 5 | 无签名/无完整性校验 | 🟡 可接受 | 建议加 sha256 |

---

## 一、阻断项：现有 wheel 是旧版

实测差异：

```
core/identity.py                    **缺失**   ← 握手 MAC、TOFU 指纹
client/webui/js/views/keybar.js     **缺失**   ← 连接状态栏
client/api/ 目录                    **缺失**   ← 新端点都在这里
```

`client/` 下只有 14 个旧文件（现仓库有 `api/`、`services/`、`webui/js/` 三个子目录）。

**影响**：如果直接发布这个包，所有已完成的安全加固——握手帧 MAC、TOFU
指纹、不自动回复握手、重放防护、载荷补满——**一个都不会生效**。

**处置**：必须先 `python -m build` 重新出 wheel，再跑 `make_client_bundle.py`。

---

## 二、阻断项：`make_bundle.py` 泄漏真实账号凭证

在旧包里实际找到：

```
snowluma/config/onebot_1741473279.json
  ├── "accessToken": "9w-avp3W-sO_sgxHqld7cwqtcQ4sL5zdNDOUPolV0UA"   ← 真实令牌
  └── 1741473279                                                        ← 真实 QQ 号

snowluma/config/webui.json              WebUI 口令哈希 + salt
snowluma/data/1741473279/               聊天数据库
snowluma/logs/snowluma-2026-10-2*.log   原文聊天日志
```

拿到 access token 就能**完全控制构建机上那个 QQ 账号的 OneBot 接口**——
收发任意消息、读取全部好友与群。

**根因**：`tools/make_bundle.py`（跨机测试包脚本）**没有**清理逻辑；
只有新的 `tools/make_client_bundle.py` 有 `_scrub_personal_state`。

**已验证新脚本正确**（造脏目录测试）：

```
清理后:  config (目录，保留但空)  index.mjs (文件)
泄漏项:  无
```

**处置**：给 `make_bundle.py` 补同样的清理，或直接从文档里撤掉它。

---

## 三、可接受项：57 个 `.py` 明文

```
.py 明文     57 个 / 622,813 字节
.pyc          0
.so/.pyd      0
```

逆向者能直接读到：

- 帧格式（`MAGIC | kind | …`）
- QIM 嵌入参数、坐标选取规则
- KDF 用途串（`handshake-mac` / `session` / `image-step` / `cover-pick`）
- 默认口令 `pbx1`

**但这不等于能解密别人的会话。** 实测分层：

```
攻击者只有默认口令和一张图：
  ✅ 能提取隐写载荷（51 字节，首字节 b'PBX1B'）
  ❌ 解不开正文 —— 会话密钥来自 X25519 临时密钥对，私钥不在图上

攻击者能观察到：
  帧类型、分片序号、密文长度（已被载荷补满掩盖）
  发了几张图（图片数量本来就暴露信息量）
```

这是**隐写层的设计边界**：口令保护的是「不被路人发现」，不是「对抗拿到
完整算法的对手」。真正的机密性靠 X25519 + AES-GCM。

**结论**：不需要混淆。Kerckhoffs 原则下，算法公开是正常的；依赖算法保密
才是脆弱的。**但文档里要写清楚这一点**，不要让用户以为「拿到源码就完了」。

---

## 四、可接受项：默认口令

源码中 11 处出现 `pbx1`：

```
cli.py:101          --psk default="pbx1"
web.py:73           psk: str = "pbx1"
client/config.py:58 psk: str = "pbx1"
terminal/terminal.py:115  psk: str = "pbx1"
…
```

**这是设计上的共享秘密**，不是身份凭据。出现在源码里不算漏洞——口令的
价值在于「双方约定」，不在于「保密」。

真正的风险是**用户不换它**。已实现两道防线：

| 位置 | 可关闭 | 定位 |
| --- | --- | --- |
| 顶部提示条 | ✅ | 日常提醒一次 |
| 设置页警告框 | ❌ | 当前事实，一直在 |

---

## 五、误报清单（不是问题）

| 发现 | 实情 |
| --- | --- |
| `3925697820` ×3 | **文档示例**里的号码，非真实账号 |
| `555555` / `666666` / `110000` | 测试假数据 |
| `onebot_config.py` / `onebot_ws.py` | 文件名命中 `/data/` 之类模式，非文件内容 |
| `webui/index.html` 含 `password` | 口令输入框的 `type="password"` |

**真实凭证/路径泄漏扫描结果：0 处**

```
绝对路径(D:/Projects)    0
用户目录(Users/death)    0
/d/_Temp                 0
token 字面量             0
```

---

## 六、建议（发布前）

### 必须做

1. **重新构建 wheel**（版本推进到 0.3.1）
2. **修 `make_bundle.py` 的凭证清理**
3. 构建后**重新跑本审计**，确认 `config/` 为空、`logs/` 不存在

### 建议做

4. **在包里附 `manifest.json` 的 sha256**，让用户能校验完整性。
   现状是客户端没有任何签名校验，攻击者改一个 `.py` 就能植入后门，
   用户无法分辨。
5. **文档加一节「威胁模型」**，明确写：

   > 本工具的隐写层保护「不被路人发现聊天里有密文」。
   > 拿到算法和聊天记录的人**能确认**你在用这个工具、
   > **能看出**你发了几张图，但**不能解出**正文内容。
   > 默认口令适合熟人之间「防止群机器人抓取」，不适合对抗有动机的对手。

### 可选

6. 考虑发布时同时给出 wheel 的 `.py` 与一份说明「源码即文档」，
   而不是试图混淆——混淆只会让审计变难，挡不住真正的逆向者。

---

## 七、审计方法（可复现）

```python
# 源码形态
zipfile.ZipFile(wheel).namelist()

# 敏感字符串
re: PRIVATE KEY / AKIA / gh[pousr]_ / QQ号 / 用户路径 / IP / 邮箱

# 运行时数据
筛选 config.json / peers.json / *.log / *.db / logs/ / data/ / onebot_*

# 协议可逆
用默认口令从真实抓包里提取隐写载荷，再试解密
```

脚本：`tools/audit_package.py`（本次新增，见提交）

---

## 八、0.3.1 打包记录（2026-10-24）

### 打包时又抓到两个 bug

#### bug 1：`package-data` 漏掉子目录 → 装上去整页空白

```toml
# 原来
markovcryptor = ["client/webui/*.html", ...]
```

`client/webui/` 拆分后是 `index.html` + `js/`（17 个）+ `css/`（5 个）共 23
个文件，而 **setuptools 的 `dir/*.ext` 不递归**。打出来的包只有 HTML。

症状是「浏览器里 JavaScript 报错」，实际是打包漏文件——从现象几乎不可能
反推到 `pyproject.toml`。

已修复，并加 `tests/test_package_assets.py`（4 项）：
- `package-data` 规则必须覆盖磁盘上每一个前端文件
- `js/`、`css/` 必须逐个被匹配
- 实体 wheel 里文件齐全
- **真实 pip 安装到临时目录，确认 `keybar.js` 等文件真的在**

#### bug 2：打完包后跑一次客户端，凭证又被写回去

打完 0.3.1 我启动客户端冒烟测试，SnowLuma 当场在
`snowluma/config/onebot_<QQ>.json` 写下**新的真实 accessToken**，还建了
`logs/`。

也就是说：**「打包时清干净」不等于「交付时干净」**。中间任何一次冒烟测试
都会把脏东西写回去。

修复：`make_client_bundle.py` 收尾时**再清一次**（幂等），然后再验证。
加两条测试：清理可重复执行、模拟「跑过之后再清」仍有效。

### 一条流程教训

**验证包时不要直接在待交付目录里跑客户端。** 更好的做法是先复制到临时
目录验证，或者验证后必须重新跑一次清理。本次用的办法：

```
打包 → 复制到 /d/_Temp/relbuild 验证 → 清理 → 复制回 dist/
```

### 最终包

```
dist/qqclient-0.3.1-20261024-2350/   216 MB
  python/    embeddable CPython 3.12.6 + 依赖
  snowluma/  config/ 为空（无账号残留）
  node/      node.exe
  start.bat / start.ps1 / manifest.json
```

实测：
- 无 JS 错误、无 4xx/5xx 资源
- 深色主题正常，默认口令提示条显示
- `core/identity.py`、`client/api/`、`keybar.js`、`tokens.css` 全在
- 全包搜旧 token / 真实 QQ 号：0 命中

**817 passed。**

---

## 九、全新安装卡在"正在启动…"（2026-10-24 用户反馈）

现象：

```
界面：正在启动…（第一次会等 QQ 登录）      一直停着
luma：WebUI listening http://127.0.0.1:5099  只有 WebUI，没有 OneBot
用户：未能识别我已登录的 QQ
```

### 根因：鸡生蛋

`onebot_<uin>.json` 的**文件名就是 QQ 号**：

* 交付包**不能**预置它 —— 里面带着构建机的真实 accessToken（见第二节）
* SnowLuma 只在 OneBot 网络**已经配好之后**才写它

于是全新安装时这个文件永远不出现，`wait_for_endpoint` 死等。用户看到
"识别不到已登录的 QQ"，实际是缺一个只能由"当前登录的是谁"推出来的配置
文件。

### 修法：问 SnowLuma 现在登录的是谁

SnowLuma 的 WebUI 有两个我们能用的接口：

```
POST /api/login     {"password": "..."} -> {"token": "..."}
GET  /api/qq-list   Bearer token        -> {"list":[{"uin":"...","nickname":"..."}]}
```

口令就是启动时通过 `SNOWLUMA_WEBUI_BOOTSTRAP_PASSWORD` 喂进去的那个。

`launcher.webui_uins()` 读出来后，`ensure_onebot_config()` 替它写配置
（已存在就不动，多个号端口递增避免 `EADDRINUSE`）。

### 第二个坑：写完必须重启 SnowLuma

补出配置后仍然失败，`netstat` 显示配置写着 3001、3001 却没监听。

原因：**SnowLuma 只在启动时读一遍配置**。我们是在它已经跑起来之后才写的
文件，它把这个文件读了一半（日志里能看到 OneBot.MessageMigration、
groups loaded 等活动），但**不会真的起 ws 服务**。

所以补完配置要重启一次 SnowLuma（只重启我们自己起的那个，
`owned` 里登记过的；用户自己开的不动）。

### 端到端验证（真实 SnowLuma + 真实登录的 QQ）

删掉 `config/` 模拟全新安装，然后启动：

```
正在等待 QQ 登录…（SnowLuma 只能挂到已登录的 QQ 上）
本机 QQ 1741473279，29 个好友，图片队列上限 9
客户端已启动：http://127.0.0.1:8741/

netstat: 127.0.0.1:3001 LISTENING   ← OneBot 起来了
```

### 测试

`tests/test_launcher_bootstrap.py`（11 项）：创建配置 / 补出的配置能被
`discover_endpoint` 认出来 / 已存在不动 / 两个号端口不撞 / 没有 config 目录
时不硬造 / 拒绝非数字 UIN / 等待期间 on_tick 会被调用 / 补完立刻返回 /
on_tick 抛异常不拖死等待。

`webui_uins` 的过滤规则用**假的 HTTP 响应跑真实代码**验证，不是重写一遍
规则（那样测的是测试自己）。

注入"不写配置"验证过 4 项会 FAIL。

**828 passed。**
