// 会话视图：消息气泡渲染、历史前插、打开会话。
//
// 增量渲染靠 R 里的三个游标（lastSeq / firstSeq / count）。整窗重建只在
// 三种情况发生：切会话、历史被前插、服务端没给 seq —— 其余都是追加。
//
// 历史消息的 seq 是**负数**，实时消息是全局递增的正数。两者不能混（曾经
// 撞过车，导致新消息被前端当成旧消息全部跳过）。

import { $, toast, clone } from "../core/dom.js";
import { api } from "../core/api.js";
import { S, COVERT } from "../core/store.js";
import { fmtTime, avatarFor, setAvatar } from "../core/format.js";

// 打开会话时要重置密文区、读状态，这些都从外部注入，避免 chat ←→ covert
// 互相 import 形成循环引用（ES module 的循环引用不报错，只会拿到 undefined）。
let hooks = {
  onOpen: () => {},        // 打开会话前重置密文区
  afterState: () => {},    // 状态刷新后更新密文区
  // 拉一次全局状态。由 app.js 注入 —— chat 不能直接 import app.js，
  // 那会形成循环引用（app.js 已经 import 了 chat），而 ES module 的
  // 循环引用不报错，只在运行时拿到 undefined。
  refreshState: async () => {},
};

export function setHooks(next) {
  hooks = Object.assign(hooks, next);
}

// 时间戳：只显示 时:分。历史消息可能跨天，跨天时补上日期。

export function bubbleFor(entry, peerName, showDate) {
  const wrap = clone("tpl-msg");               // 结构见 index.html 的 <template>
  if (!wrap) return null;
  wrap.classList.add(entry.mine ? "mine" : "them");
  wrap._entry = entry;                         // 右键菜单靠它取原文

  // 谁说的 + 什么时候，放在气泡上方。
  wrap.querySelector(".who").textContent = entry.mine ? "我" : (peerName || "对方");
  wrap.querySelector(".stamp").textContent = (() => {
    const stamp = fmtTime(entry.at, showDate);
    return stamp ? " · " + stamp : "";
  })();

  const b = wrap.querySelector(".bubble");
  if (entry.text) {
    const t = document.createElement("div");
    t.textContent = entry.text;
    b.appendChild(t);
  }
  for (const src of entry.images || []) {
    const img = document.createElement("img");
    img.src = src;
    b.appendChild(img);
  }
  for (const line of entry.unsupported || []) {
    appendLine(b, "unsupported", line);
  }
  for (const line of entry.notes || []) {
    appendLine(b, "note", line);
  }
  if (!b.hasChildNodes()) return null;         // 空条目不画，避免空气泡
  return wrap;
}

function appendLine(parent, className, text) {
  const el = document.createElement("div");
  el.className = className;
  el.textContent = text;
  parent.appendChild(el);
}

// 增量渲染状态。每 2.5 秒重建整棵 DOM 的代价是：用户滚上去看历史会被
// 强行拉回底部、正在复制的选区丢失、消息越多越卡。所以按 seq 记住渲染到
// 哪一条，只 append 新的。切会话时才整窗重建。
// lastSeq 用 null 表示"还没渲染过"，**不能用 -1**。
// 历史条目的 seq 是负数（见 conversations.prepend_history：负数天然排在
// 实时消息之前，也不会和 _ARRIVAL 分配的正数冲突），用 -1 当哨兵会把
// 所有历史当成"已渲染"跳过。
export const R = { peer: null, lastSeq: null, firstSeq: null, count: 0 };

export function resetRender(peer) {
  R.peer = peer;
  R.lastSeq = null;
  R.firstSeq = null;
  R.count = 0;
  $("plainThread").innerHTML = "";
  $("covertThread").innerHTML = "";
}

// 用户是否本来就贴着底部。只有这种情况才自动滚到底 —— 用户往上翻历史时
// 新消息到达不该把他拽回去。
export function nearBottom(el) {
  return el.scrollHeight - el.scrollTop - el.clientHeight < 80;
}

export function covertBubbleFor(entry, peerName, showDate) {
  const cw = document.createElement("div");
  cw.className = "msg " + (entry.mine ? "mine" : "them");
  const cmeta = document.createElement("div");
  cmeta.className = "meta";
  const cwho = document.createElement("span");
  cwho.className = "who";
  cwho.textContent = entry.mine ? "我" : (peerName || "对方");
  cmeta.appendChild(cwho);
  const cstamp = fmtTime(entry.at, showDate);
  if (cstamp) {
    cmeta.append(document.createTextNode(" · "));
    const t = document.createElement("span");
    t.textContent = cstamp;
    cmeta.appendChild(t);
  }
  cw.appendChild(cmeta);
  const cb = document.createElement("div");
  cb.className = "bubble";
  cb.textContent = entry.covert.join("\n");
  cw.appendChild(cb);
  return cw;
}

export function renderHeader(data) {
  // 对端头像：优先用好友列表里的头像，未知联系人按号码现拼一个。
  const friend = (S.friends || []).find((f) => f.uin === data.peer);
  const name = data.name || data.peer;
  setAvatar($("peerAvatar"), (friend && friend.avatar) || avatarFor(data.peer), name);
  $("peerName").textContent = name;
  const st = $("peerState");
  st.innerHTML = "";
  const span = document.createElement("span");
  span.className = data.established ? "est" : "wait";
  span.textContent = data.established ? "信道已建立" : data.state_label;
  st.appendChild(span);
}

// 把 entries 里还没渲染的部分追加进去。整窗重建只发生在三种情况：
// 切会话、历史被前插、服务端没给 seq。
export function renderConversation(data) {
  const peer = data.peer;
  if (R.peer !== peer) resetRender(peer);
  renderHeader(data);

  const entries = data.entries || [];
  const plain = $("plainThread");
  const covert = $("covertThread");

  const seqs = entries.map((e) => e.seq);
  const hasSeq = seqs.every((v) => typeof v === "number");
  const firstSeq = hasSeq && seqs.length ? seqs[0] : null;
  // 历史前插：最早那条比我们渲染过的还早，说明窗口头部多了内容。
  const prepended = hasSeq && R.lastSeq !== null && firstSeq !== null &&
                    firstSeq < R.firstSeq;
  const shrank = entries.length < R.count;
  const rebuild = !hasSeq || prepended || shrank;

  const plainAtBottom = nearBottom(plain);
  const covertAtBottom = nearBottom(covert);

  if (rebuild) {
    plain.innerHTML = "";
    covert.innerHTML = "";
    R.lastSeq = null;
    R.firstSeq = null;
    R.count = 0;
  }

  const showDate = new Set(entries.filter((e) => e.at)
    .map((e) => new Date(e.at * 1000).toDateString())).size > 1;

  for (const entry of entries) {
    if (!rebuild && typeof entry.seq === "number"
        && R.lastSeq !== null && entry.seq <= R.lastSeq) {
      continue;                       // 已经渲染过
    }
    const b = bubbleFor(entry, data.name, showDate);
    if (b) plain.appendChild(b);
    if ((entry.covert || []).length) {
      covert.appendChild(covertBubbleFor(entry, data.name, showDate));
    }
    if (typeof entry.seq === "number") {
      R.lastSeq = (R.lastSeq === null)
        ? entry.seq
        : Math.max(R.lastSeq, entry.seq);
    }
  }
  if (hasSeq && seqs.length) {
    R.firstSeq = R.firstSeq === null ? seqs[0] : Math.min(R.firstSeq, seqs[0]);
  }
  R.count = plain.childElementCount;

  // 只在用户本来就贴着底部时才跟随；往上翻历史时不该被拽回去。
  if (plainAtBottom) plain.scrollTop = plain.scrollHeight;
  if (covertAtBottom) covert.scrollTop = covert.scrollHeight;
}

export async function openConversation(peer) {
  // 换会话时重置握手重试计数和遮挡层：每个好友的信道是独立的。
  hooks.onOpen();
  S.current = peer;
  $("empty").hidden = true;
  $("chat").hidden = false;
  try {
    await api(`/client/conversation/${peer}/open`, { method: "POST" });
    // 首次打开拉一次 QQ 历史；服务端只合并一次，重复点不会重复灌。
    const data = await api(`/client/conversation/${peer}?history=20`);
    renderConversation(data);
    await hooks.refreshState();
  } catch (e) {
    toast(e.message);
  }
}
