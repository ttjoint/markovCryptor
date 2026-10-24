// 应用层：状态刷新、发送动作、空状态引导、左右分栏、存活探测。
//
// 这一层是「粘合剂」：它知道所有视图模块，负责装载它们、注入依赖、
// 处理跨视图的动作。视图模块之间从不互相 import。

import { $, toast } from "./core/dom.js";
import { api } from "./core/api.js";
import { S, COVERT } from "./core/store.js";
import * as Idbar from "./views/idbar.js";
import * as List from "./views/list.js";
import * as Chat from "./views/chat.js";
import * as Overlays from "./views/overlays.js";
import * as Composer from "./views/composer.js";
import * as Covert from "./views/covert.js";
import * as Settings from "./panels/settings.js";
import * as SendQueue from "./views/sendqueue.js";
import * as StatusBar from "./views/statusbar.js";
import * as Capacity from "./views/capacity.js";

// ---------------------------------------------------------------- 状态刷新

// 轮询失败时只在"状态发生变化"时提示一次：每 2.5 秒弹一个 toast 会刷屏，
// 但完全静默会让用户对着一个再也不更新的界面发呆，分不清是卡了还是没人说话。
let lastStateError = null;

// 拆成两半：applyState 只负责把数据画到界面上，取数据由调用方负责。
// 这样存活探测和内容刷新能复用同一段渲染逻辑，又不会重复请求。
export function applyState(data) {
  if (lastStateError) lastStateError = null;
  S.uin = data.uin;
  S.ready = data.ready;
  S.conversations = data.conversations;
  S.friends = data.friends;

  // 标签页计数：最近 (n) / 好友 (n)。
  List.renderCounts(data);

  // 身份栏（头像/昵称/QQ号/在线点）+ 窗口标题。
  Idbar.renderSelf(data);

  // 底部状态栏：握手/文本排队/图片排队/接收积压。
  // 和主状态同频刷新，不额外发请求。
  StatusBar.render(data.tasks, data);

  const q = data.queue;
  $("queueStat").textContent = `队列 ${q.queued}/${q.max}`;
  Composer.renderQueue(q.items);
  $("queueHint").textContent = q.folders === 0
    ? "未配置图片文件夹" : `${q.folders} 个文件夹`;

  const m = data.models || { total: 0, loaded: 0, names: [], loading: false };
  S.textUsable = !!data.text_channel;
  S.modelNames = m.names || [];
  // modelStat 已并入信道栏提示，这里只在元素存在时更新。

  // 模型状态变了要重新渲染信道控件：从"无模型"变成"有模型"时，
  // 开关要恢复可切换、下拉要出现。否则用户配好模型后界面还停在旧样子。
  Covert.renderChannelExtras({ textUsable: S.textUsable, modelNames: S.modelNames });

  // 会话状态决定遮挡层。已建立则露出输入区。
  if (S.current) {
    const conv = (data.conversations || []).find((c) => c.peer === S.current);
    const established = !!(conv && conv.established);
    S.established = established;

    if (conv && conv.pending && !established) {
      // 对方在请求建立连接，等用户决定。指纹等 refreshConversation 拿回来
      // 再填 —— 这里先用已知信息把遮挡层换掉，避免闪一下"尚未握手"。
      Covert.setCovertState("pending", S.handshakeInfo || {});
    } else if (established) {
      Covert.setCovertState("ready");
      S.handshakeInfo = null;
    } else {
      Covert.setCovertState(COVERT.state === "waiting" ? "waiting" : "locked");
      S.handshakeInfo = null;
    }
  }

  updateOnboard(data);
  List.renderRows();

  // 图片信道的容量检查。放这里是因为它依赖队列内容，而队列每轮都可能变
  // （用户往文件夹里加了图）。只提示，不禁用 —— 文本信道仍可能可用。
  if (S.current && S.established && COVERT.channel === "image") {
    const cap = data.handshake_capacity || null;
    if (cap && !cap.enough) {
      Covert.setCovertState("noimage", {
        message: cap.queued === 0
          ? "待发图片队列是空的。配置图片文件夹，或点「补图」。"
          : `队列里 ${cap.queued} 张图，最多只能藏 ${cap.capacity} 字节，装不下。`,
      });
    } else if (COVERT.state === "noimage") {
      Covert.setCovertState("ready");
    }
  }

}

// 拉当前会话的消息。单独一个函数，因为它会发请求 —— applyState 只画界面。
export async function refreshConversation() {
  if (!S.current) return;
  try {
    const detail = await api(`/client/conversation/${S.current}`);
    Chat.renderConversation(detail);
    // 待确认的握手要显示对方指纹 —— 那正是让用户判断"这是不是本人"的东西。
    // 指纹只在会话详情里，所以拿到后补画一次遮挡层。
    const hs = detail.handshake || {};
    S.handshakeInfo = hs;
    if (hs.pending && COVERT.state === "pending") {
      Covert.setCovertState("pending", hs);
    }
  } catch (e) {
    // 会话可能已被丢弃；但要让人知道这次刷新没成功，否则新消息静默不出现。
    if (lastStateError !== e.message) {
      lastStateError = e.message;
      toast("读取会话失败：" + e.message);
    }
  }
}

// 兼容旧调用点：取数据 + 画界面 + 刷新当前会话。
export async function refreshState() {
  try {
    const data = await api("/client/state");
    applyState(data);
    await refreshConversation();
  } catch (e) {
    if (backendGone(e)) { shutdownWindow("后端已退出"); return; }
    if (lastStateError !== e.message) {
      lastStateError = e.message;
      toast("连接异常：" + e.message);
    }
  }
}

// ---------------------------------------------------------------- 动作

// 明文发送：文本 + 附件合并成**一条**消息。
// QQ 允许只发图片，所以文本为空但有附件时也要发。
export async function sendPlain() {
  if (!S.current) return;
  // 正在发送时按钮就是"取消"，这一点由 sendqueue 负责改写按钮文案。
  if (SendQueue.busy()) {
    await SendQueue.cancelCurrent();
    return;
  }
  const text = $("input").value;
  const images = Composer.ATTACH.items.map((item) => item.data);
  if (!text.trim() && !images.length) return;

  // 先清空输入框：内容已经交给后台了。失败也不还原 —— 那样反而让人
  // 以为"没发出去所以还能改"，而实际上任务可能已经在上传。
  const peer = S.current;
  Composer.clearInput("input");
  Composer.ATTACH.items = [];
  Composer.renderAttach();

  await SendQueue.submit(
    { peer, mode: "text", text, images },
    {
      onDone: async () => {
        const detail = await api(`/client/conversation/${peer}`);
        Chat.renderConversation(detail);
        await refreshState();
      },
    },
  );
}

// 密文发送：走当前选中的信道。
export async function sendCovert() {
  if (!S.current) return;
  if (SendQueue.busy()) {
    await SendQueue.cancelCurrent();
    return;
  }
  const text = $("covertInput").value;
  if (!text.trim()) {
    toast("密文内容不能为空");
    return;
  }
  // 装不下就别提交：编码要 0.6-3.3 秒，白等一次再报错是浪费用户时间。
  const estimate = Capacity.latestEstimate();
  if (estimate && !estimate.ok) {
    toast(estimate.reason || "当前内容装不下，请看输入框下方的提示");
    return;
  }
  const peer = S.current;
  Composer.clearInput("covertInput");
  Capacity.clear();

  await SendQueue.submit(
    { peer, mode: "covert", text, channel: COVERT.channel },
    {
      onDone: async () => {
        const detail = await api(`/client/conversation/${peer}`);
        Chat.renderConversation(detail);
        await refreshState();
      },
    },
  );
}

export async function handshakeLegacy() {
  if (!S.current) return;
  try {
    await api("/client/send", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ peer: S.current, mode: "handshake" }),
    });
    const detail = await api(`/client/conversation/${S.current}`);
    Chat.renderConversation(detail);
    await refreshState();
  } catch (e) {
    toast(e.message);
  }
}

export async function refill() {
  try {
    const data = await api("/client/queue/refill", { method: "POST" });
    toast(`补入 ${data.added} 张${data.answered ? `，补发握手 ${data.answered}` : ""}`);
    await refreshState();
  } catch (e) {
    toast(e.message);
  }
}

// ---------------------------------------------------------------- 事件绑定

$("tabRecent").onclick = () => List.setMode("recent");
$("tabFriends").onclick = () => List.setMode("friends");

$("btnSendText").onclick = sendPlain;
$("btnSendCovert").onclick = sendCovert;
$("btnRefill").onclick = refill;
$("veilAction").onclick = () => {
  if (COVERT.state === "noimage") { refill(); return; }
  Covert.doHandshake();
};

$("btnModelReload").onclick = async () => {
  try {
    const d = await api("/client/models/reload", { method: "POST" });
    toast(`重新扫描：已加载 ${d.loaded}/${d.total}`);
    await refreshState();
  } catch (e) { toast(e.message); }
};

$("modelPick").onchange = async () => {
  const name = $("modelPick").value;
  if (!name) return;
  try {
    await api("/client/models/select", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name }),
    });
    toast("已切换到 " + name);
  } catch (e) { toast(e.message); }
};

// 两个输入框各自 Enter 发送。Shift+Enter 换行 —— 和 QQ 一致。
$("input").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
    e.preventDefault();
    sendPlain();
  }
});

$("covertInput").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
    e.preventDefault();
    sendCovert();
  }
});

// 边输入边算容量。300ms 防抖，数字来自后端逐张真实计算。
$("covertInput").addEventListener("input", () => Capacity.schedule());

// 装不下时禁用发送 —— 让用户当场知道，而不是点了之后白等一次编码。
// 只在「确实算过、且确实装不下」时禁用；估算失败不禁用（真正的校验在
// 后端 /client/send 还会做一遍，这里只是提前告知）。
Capacity.setBlocker((blocked) => {
  const btn = $("btnSendCovert");
  if (!btn) return;
  // 下面两种情况下按钮的 disabled 不归容量条管：
  //   locked/waiting —— 未握手，covert.updateCovertControls() 统一禁用
  //   发送中         —— 按钮已经变成"取消"，点了要能取消
  // 抢着改会让状态互相覆盖（实测：未握手时容量条把按钮解锁了）。
  if (COVERT.state === "locked" || COVERT.state === "waiting") return;
  if (btn.classList.contains("cancel")) return;
  btn.disabled = blocked;
  btn.title = blocked ? "当前内容装不下，请看下方提示" : "隐藏并发送（Enter）";
});




// ================================================================= 空状态引导

export function updateOnboard(data) {
  const q = data.queue || {};
  const m = data.models || {};
  const qEl = $("obQueue");
  if (qEl) {
    qEl.innerHTML = "";
    qEl.append("图片文件夹：");
    const b = document.createElement("b");
    if ((q.folders || 0) > 0) { b.textContent = `已配置 ${q.folders} 个`; b.style.color = "var(--ok)"; }
    else b.textContent = "未配置";
    qEl.appendChild(b);
    qEl.append(" —— 握手和图片密文都需要它");
  }
  const mEl = $("obModel");
  if (mEl) {
    mEl.innerHTML = "";
    mEl.append("文本模型：");
    const b = document.createElement("b");
    if ((m.loaded || 0) > 0) { b.textContent = `已加载 ${m.loaded} 个`; b.style.color = "var(--ok)"; }
    else b.textContent = "未加载";
    mEl.appendChild(b);
    mEl.append(" —— 文本密文需要它");
  }
}

// ================================================================= 左右分栏

// 拖动分隔条改宽度；双击恢复均分；太窄就整栏收起。
// 宽度存 localStorage：这是界面偏好，不是聊天内容，落盘没有隐私问题。
const SPLIT = {
  key: "mkx.split",
  ratio: 0.5,
  minPx: 220,
  dragging: false,
};

export function applySplit() {
  const panes = $("panes");
  const plain = $("panePlain");
  const covert = $("paneCovert");
  const splitter = $("splitter");
  const total = panes.clientWidth - 6;         // 减掉分隔条
  if (total <= 0) return;

  // 塌陷后仍然要能拖回来，所以分隔条**永远不隐藏**：
  // 被藏掉的是那一侧的 pane，分隔条留在剩下那栏的边缘上。
  // 拖它一下，ratio 就离开 0/1 区间，那一侧自动重新出现。
  const plainPx = Math.round(total * SPLIT.ratio);
  const tooNarrow = plainPx < SPLIT.minPx || (total - plainPx) < SPLIT.minPx;

  if (tooNarrow) {
    const hidePlain = plainPx < total - plainPx;
    plain.classList.toggle("collapsed", hidePlain);
    covert.classList.toggle("collapsed", !hidePlain);
    (hidePlain ? covert : plain).style.flex = "1 1 100%";

    // 分隔条贴到剩下那一栏的哪一边？被藏的是左侧 -> 贴左边缘，反之贴右。
    splitter.classList.toggle("at-left", hidePlain);
    splitter.classList.toggle("at-right", !hidePlain);
    // 收起态给个明显的抓手，否则一条 6px 的线用户不知道能拖。
    splitter.classList.add("collapsed");
    splitter.title = hidePlain
      ? "向右拖动以显示明文栏（双击恢复均分）"
      : "向左拖动以显示密文栏（双击恢复均分）";
    return;
  }

  splitter.classList.remove("at-left", "at-right", "collapsed");
  splitter.title = "拖动调整左右宽度，双击恢复均分";
  plain.classList.remove("collapsed");
  covert.classList.remove("collapsed");
  plain.style.flex = `0 0 ${plainPx}px`;
  covert.style.flex = "1 1 0";
}

export function initSplitter() {
  const splitter = $("splitter");
  const panes = $("panes");

  const saved = Number(localStorage.getItem(SPLIT.key));
  if (saved >= 0 && saved <= 1) SPLIT.ratio = saved;

  splitter.addEventListener("mousedown", (e) => {
    SPLIT.dragging = true;
    splitter.classList.add("dragging");
    document.body.style.cursor = "col-resize";
    e.preventDefault();
  });

  window.addEventListener("mousemove", (e) => {
    if (!SPLIT.dragging) return;
    const box = panes.getBoundingClientRect();
    const ratio = (e.clientX - box.left) / Math.max(1, box.width - 6);
    // 允许拖到 0/1 之外一点点：这代表"收起这一侧"。
    // 拖回来时 ratio 离开极值，applySplit 会让它重新出现。
    SPLIT.ratio = Math.min(1, Math.max(0, ratio));
    applySplit();
  });

  window.addEventListener("mouseup", () => {
    if (!SPLIT.dragging) return;
    SPLIT.dragging = false;
    splitter.classList.remove("dragging");
    document.body.style.cursor = "";
    localStorage.setItem(SPLIT.key, String(SPLIT.ratio));
  });

  splitter.addEventListener("dblclick", () => {
    SPLIT.ratio = 0.5;
    localStorage.setItem(SPLIT.key, "0.5");
    applySplit();
  });

  window.addEventListener("resize", applySplit);
  applySplit();
}

// ================================================================= 头像

// 头像走 QQ 的公开 CDN。加载失败时保留"首字占位圆"，不要留一个破图图标。
// 用 dataset 记住当前 URL，避免每次轮询（1.5 秒一次）都重新赋值 src ——
// 那会让图片反复闪烁，也会浪费流量。


// ================================================================= 前后端同生共死

// 前后端是一体的：后端退出，窗口就该消失；窗口关掉，后端就该退出。
// 不显示"未连接"这种中间态 —— 能看见界面就说明连着，看不见就是结束了。

let shuttingDown = false;

// 判定"后端真的没了"而不是"这一次请求碰巧失败"。
// 只有拿不到任何 HTTP 响应才算（网络层错误）；HTTP 4xx/5xx 说明服务还活着。
function backendGone(error) {
  const text = String((error && error.message) || error || "");
  // fetch 在连不上时抛 TypeError: Failed to fetch / NetworkError
  return /failed to fetch|networkerror|load failed|connection|ECONNREFUSED|超时/i
    .test(text);
}

// 关掉这个窗口。浏览器 --app 模式下 window.close() 生效；
// 普通标签页里浏览器会拒绝，那就退化成"把界面清空 + 显示一句话"，
// 总比留着一个假界面好。
function shutdownWindow(reason) {
  if (shuttingDown) return;
  shuttingDown = true;
  if (PROBE.timer) { clearTimeout(PROBE.timer); PROBE.timer = null; }
  // 先把界面变成已退出，再尝试关窗。
  // 顺序很重要：普通标签页里 window.close() 会被拒绝，如果只靠它，
  // 用户会对着一个假界面发呆。先改界面，无论关不关得掉都是诚实的。
  document.body.innerHTML =
    '<div style="display:grid;place-items:center;height:100vh;'
    + 'font:14px system-ui;color:#888;text-align:center">'
    + '<div><div style="font-size:20px;margin-bottom:8px">已退出</div>'
    + '<div>' + reason + '。可以关闭此页面。</div></div></div>';
  try { window.close(); } catch (_) { /* 普通标签页会拒绝 */ }
}

// 用户主动退出：先告诉后端（它会收掉 SnowLuma 等），再关窗口。
async function quitApp() {
  if (!confirm("退出客户端？\n\n这会断开与 QQ 的连接并关闭后台进程。")) return;
  shuttingDown = true;
  if (PROBE.timer) { clearTimeout(PROBE.timer); PROBE.timer = null; }
  try { await api("/client/quit", { method: "POST" }); } catch (_) { /* 已经在退 */ }
  document.body.innerHTML =
    '<div style="display:grid;place-items:center;height:100vh;'
    + 'font:14px system-ui;color:#888">已退出。可以关闭此页面。</div>';
  try { window.close(); } catch (_) { /* ignore */ }
}

// 判断"后端还在吗"。
//
// 试过 SSE，实测**更慢**：后端进程被强杀时没有 FIN/RST 及时送达，浏览器要等
// TCP 超时（实测 5 秒）才触发 onerror。轮询反而更快 —— 一次 fetch 打到一个
// 已经死掉的端口，几十毫秒就返回 ECONNREFUSED。
//
// 所以这里用**自适应轮询**：平时 4 秒一次（只为刷新内容），一旦有一次失败就
// 切到 200ms 快速探测，确认死了立刻退出，恢复了再回到 4 秒。
const PROBE = {
  // 慢速周期决定了两件事：内容多久刷新一次，以及后端死掉后最坏多久被发现。
  // 1.5 秒是本机回环请求，代价可以忽略；再快也不会有明显收益，因为
  // 真正的瓶颈是"要确认两次才敢关窗"，不是这个间隔。
  slow: 1500,
  timer: null,
};

export function scheduleProbe(delay) {
  if (PROBE.timer) clearTimeout(PROBE.timer);
  if (shuttingDown) return;
  PROBE.timer = setTimeout(async () => {
    PROBE.timer = null;
    const alive = await probeOnce();
    // 探测失败时立刻再确认一次（不等下一个周期）：一次失败可能是重启中的
    // 瞬时状态，两次连续失败基本可以断定后端没了。这个"立即重试"比把
    // 轮询周期压到几百毫秒有效得多。
    if (alive === false) {
      scheduleProbe(120);
      return;
    }
    scheduleProbe(PROBE.slow);
  }, delay);
}

// 一次探测 = 一次请求。不要在这里再调 refreshState()：
// 那会重复发一遍 /client/state，而且失败时两条路径都会去判"后端没了"，
// 既慢又容易重复触发。
// 返回 true = 还活着；false = 这次连不上（调用方决定要不要再确认）。
export async function probeOnce() {
  let data;
  try {
    data = await api("/client/state");
  } catch (e) {
    if (backendGone(e)) {
      // 超时/连不上就直接判定后端已退出，不再"二次确认"。
      //
      // 曾经要求连续两次失败才关窗，理由是"后端可能在重启"。但实测这会让
      // 退出延迟翻倍（1.2 秒 -> 2.4 秒），而"后端正在重启"这个场景在本程序
      // 里并不存在：后端是自己起的，它退出就意味着这次会话结束了。
      // 真正需要容错的瞬时抖动（某个请求 500）走下面的 toast 分支。
      shutdownWindow("后端已退出");
      return false;
    }
    toast("连接异常：" + e.message);
    return true;                        // 服务活着，只是这次请求出错
  }
  applyState(data);
  await refreshConversation();
  return true;
}

export function initLifecycle() {
  // 页面正在关闭时也给后端一个信号，比等心跳超时快得多。
  window.addEventListener("pagehide", () => {
    if (shuttingDown) return;
    try {
      navigator.sendBeacon("/client/quit");
    } catch (_) { /* 尽力而为 */ }
  });
  scheduleProbe(0);
}


    toast("已忽略对方的连接请求");
    await refreshState();
  } catch (e) {
    toast(e.message);
  }
};