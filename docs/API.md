# Web API

本机服务，无鉴权，默认只监听 `127.0.0.1:8731`。

```powershell
python -m markovcryptor.cli serve              # 默认 127.0.0.1:8731
python -m markovcryptor.cli serve --port 9000  # 换端口
$env:MKX1_PORT=9000; python -m markovcryptor.cli serve
```

交互文档在 <http://127.0.0.1:8731/docs>。

- [1. 给 agent 的最小调用序列](#1-给-agent-的最小调用序列)
- [2. 端点](#2-端点)
- [3. 错误](#3-错误)
- [4. 安全边界](#4-安全边界)
- [5. 观测](#5-观测)

---

## 1. 给 agent 的最小调用序列

**先问服务器能做什么，再决定怎么做**——不要硬编码信道名和参数名。

### 1.1 一次嵌入与提取

```bash
# 1. 这个服务支持哪些信道、每个信道要什么参数
curl -s http://127.0.0.1:8731/channels

# 2. 这张图能装下我的载荷吗（不落盘、不需要口令）
curl -s -X POST "http://127.0.0.1:8731/probe?channel=image&payload_bytes=2000&kind=image" \
  -F "file=@cover.png"

# 3. 嵌入
curl -s -X POST "http://127.0.0.1:8731/embed?channel=image&key=secret" \
  -F "payload_file=@secret.bin" \
  -F "cover_file=@cover.png" \
  -o hidden.png

# 4. 提取
curl -s -X POST "http://127.0.0.1:8731/extract?channel=image&key=secret" \
  -F "file=@hidden.png" \
  -o recovered.bin
```

### 1.2 只看 schema 就能写出正确的调用

`GET /channels` 返回的每一项都带 `options_schema`（标准 JSON Schema）。
`embed` / `extract` 的 `options` 是这个 schema 的一个实例，作为 **query 里的
JSON 字符串**传入：

```bash
# options={"step":14}  —— 换行只是为了这里好读，实际是一条命令
curl -s -X POST \
  'http://127.0.0.1:8731/embed?channel=image&key=k&options=%7B%22step%22%3A14%7D' \
  -F "payload_file=@s.bin" -F "cover_file=@c.png" -o out.png
```

用编程语言调用时直接传原始 JSON，不必手工编码。

**`null` 表示"让信道自己决定"**。GIF 的 `step` 默认 `null`，意思是自动从 20
试到 128。要自动就别把这个键放进 `options`；传 `0` 会被拒绝为越界。

### 1.3 响应里的报告

`/embed` 和 `/extract` 的响应体是**二进制**，报告在响应头里：

| 头 | 含义 |
|---|---|
| `X-Payload-Bytes` | 载荷字节数 |
| `X-Capacity-Bytes` | 载体容量 |
| `X-Utilization` | 利用率 0~1 |
| `X-Option-*` | 实际生效的参数（仅 ASCII 值） |
| `X-Report` | 完整报告 JSON（`ensure_ascii`，可直接 `json.loads`） |

**为什么参数要回传**：QIM step 会自动升档、小图会自动放大。调用方必须知道
最终用的是什么，否则复现不了、也解释不了为什么输出比输入大。

**为什么中文口令不进 `X-Option-*`**：HTTP 头只能是 latin-1，放中文会抛
`UnicodeEncodeError` 把成功请求变成 500。非 ASCII 值只出现在 `X-Report` 里。

### 1.4 批处理

`POST /jobs` 的全部参数（除载荷）都在 **query** 里，因为路径是**服务端**解析的：

```bash
# input_dir / output_dir 必须 URL 编码（中文与反斜杠）
curl -s -X POST \
  "http://127.0.0.1:8731/jobs?channel=image&action=embed&key=secret&input_dir=D%3A%5CPictures%5C%E8%A1%A8%E6%83%85%E5%8C%85&output_dir=D%3A%5Cout" \
  -F "payload_file=@secret.bin"
# → {"job_id": "a1b2c3", "total": 15488, "state": "running"}
```

可选 query：`limit`、`workers`（1~64，默认 4）、`on_error`（`skip`/`abort`/`retry`）、
`resume`（断点 JSONL 路径）、`kind`、`options`。

```bash
curl -s http://127.0.0.1:8731/jobs/<job_id>                 # 进度
curl -s "http://127.0.0.1:8731/jobs/<job_id>/report?failures_only=true"
curl -s -X POST http://127.0.0.1:8731/jobs/<job_id>/cancel
```

作业在服务端后台线程跑，**关掉客户端不影响**。`resume` 指向的 JSONL 是追加式
断点，中断后带同一个 `resume` 重新提交会跳过已完成的项。

**为什么用文件夹而不是 500 个上传**：调用方和服务按构造就在同一台机器上
（只能绑回环），指一个目录既简单又不用把几个 GB 通过 HTTP 搬一遍。

---

## 2. 端点

### `GET /health`

```json
{"ok": true, "channels": ["image", "gif", "chinese"], "models": 3}
```

### `GET /channels`

每个信道的 id、显示名、`mime_in` / `mime_out`、`options_schema`。

### `POST /probe`

query：`channel`、`payload_bytes`、`kind`（`image`/`animation`/`text`）。
body：`file`（或 `text`，中文信道可为空）。

返回 `ok`、`capacity_bytes`、`required_bytes`、`utilization`、`reasons`、`hints`、`cost`。

**`probe` 不接受 `options`**：它回答的是"这个载体装得下这么多字节吗"，
与参数无关。想得到自动升档/放大后的结论，直接 `embed` 一次。

### `POST /embed`

query：`channel`、`key`、`kind`、`options`。
body：`payload_file`（要藏的东西）+ `cover_file`（载体）。

**两个上传字段分开**，不共用：中文信道没有载体，共用一个字段只能靠约定区分。

### `POST /extract`

query：`channel`、`key`、`kind`、`options`。
body：`file`（载体）或 `text`。

**文件优先，`text` 兜底**。两者都给时用文件。

### 作业

| 端点 | 说明 |
|---|---|
| `POST /jobs` | 提交，`202`，返回 `job_id` 与 `total` |
| `GET /jobs` | 最近 50 个作业的摘要 |
| `GET /jobs/{id}` | 单个作业的进度 |
| `POST /jobs/{id}/cancel` | 取消，已完成的项保留 |
| `GET /jobs/{id}/report` | 完整报告，`failures_only=true` 只列失败项 |

### `GET /metrics`

见 §5。

---

## 3. 错误

所有错误是同一形状：

```json
{
  "code": "capacity_exceeded",
  "message": "容量不足：最多 90 字节，需要 500 字节",
  "reasons": ["128×128 只能容纳 90 字节…"],
  "hints": ["换一张更大的图，或减小载荷"]
}
```

| `code` | HTTP | 含义 |
|---|---|---|
| `invalid_input` | 422 | 参数错、文件空、`options` 不是 JSON 对象 |
| `unsupported_carrier` | 415 | 载体格式不支持 |
| `capacity_exceeded` | 413 | 装不下 |
| `bad_key` | 401 | 密钥/口令错 |
| `no_hidden_frame` | 404 | 没有隐写帧 |
| `carrier_damaged` | 422 | 载体被裁剪或重压缩 |
| `channel_unavailable` | 503 | 中文信道缺模型 |

**`reasons` 保留原文，不笼统化**。要展示给用户就直接展示，不要替换成
"操作失败"——原文才是能让人修好问题的东西。

---

## 4. 安全边界

### 无鉴权，这是有意的

调用方是同机程序。**能连上 `127.0.0.1:8731` 的进程，本来就能读这个用户的
密钥文件**，token 挡不住它。token 只会增加"从哪来的 / 为什么变了"这类故障点，
不增加实际安全性。

未做的、以及为什么：

| 没做 | 理由 |
|---|---|
| token / API key | 见上：不增加安全性，只增加故障点 |
| 限流 | 单机自用；限流只会让批处理变慢 |
| CSRF 校验 | 无 Cookie、无会话，没有 CSRF 面 |
| 请求签名 | 同机进程可用同一份密钥，签名验不出区别 |

### 只允许监听回环地址

`run_server` 在非回环地址上**直接拒绝启动**：

```
拒绝在 0.0.0.0 上监听：本服务无鉴权，只应绑定回环地址
```

CLI **没有绕过开关**——意外把服务暴露到局域网需要改代码，而不是加一个参数。
代价是"想从另一台机器用"必须自己搭隧道（SSH 端口转发即可）。

### 上传上限 64 MB

不是安全措施，是防止一张 500 MB 的图被解码成几十 GB 像素拖死进程。

---

## 5. 观测

### 日志

```powershell
python -m markovcryptor.cli --log-level DEBUG serve
python -m markovcryptor.cli --log-file D:\logs\mkx1.log serve
$env:MKX1_LOG_LEVEL = "DEBUG"
```

单行 `key=value`：

```
2026-10-19 02:29:02 INFO embed channel=image payload_bytes=2 carrier_bytes=45026 elapsed_ms=67.4
```

**为什么不是 JSON**：本项目的消息是中文、操作者在终端里读。
JSON 会让"第 300 项为什么失败"必须经过解析器才能回答。
`key=value` 稳定且可机器解析，含空格的值加引号。

### `/metrics`

```json
{
  "ok": true,
  "uptime_s": 124.5,
  "counters": {
    "requests{route=/embed,status=200}": 12,
    "requests_failed{code=capacity_exceeded}": 3,
    "embed{channel=image}": 12
  },
  "latency": {
    "request_ms{route=/embed}": {"count": 12, "mean_ms": 84.3},
    "embed_ms{channel=image}": {"count": 12, "mean_ms": 81.7}
  }
}
```

- 标签用**路由模板**而非原始路径。否则 `/jobs/<uuid>` 会为每个作业建一个计数器，
  指标就没法读了。
- **`GET /metrics` 不给自己计数**：否则轮询的抓取器会把自己的数字刷成主体。
- 延迟记录 count + sum，能算均值，不保留样本——这个服务的规模不需要分位数。
- 不做 Prometheus 文本格式：会诱导抓取器依赖这个无鉴权的本机服务，
  而单机操作者从格式里得不到任何好处。

进程内计数，重启清零。
