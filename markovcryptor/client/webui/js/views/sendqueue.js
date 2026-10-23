// 发送任务的提交、轮询与取消。
//
// 后端把编码放到了后台（Markov 0.6–0.75s、1600² 嵌图 3.3s），所以
// `/client/send` 现在只返回 task_id。这里负责把 task 走完，并且：
//
// * 同一会话在途期间**禁用**发送按钮（顺序不能靠猜）
// * 按钮变转圈，旁边给一个取消
// * 取消后已编码未发出的图归还队列（后端负责，这里只报告结果）

import { $, toast } from "../core/dom.js";
import { api } from "../core/api.js";
import { S } from "../core/store.js";

//: 正在发送的任务：peer -> {taskId, timer, button, ...}
const ACTIVE = new Map();

/** 这个会话是不是正在发？界面靠它禁用按钮。 */
export function busy(peer) {
  return ACTIVE.has(peer ?? S.current);
}

/** 当前会话的任务（若有）。 */
export function current() {
  return ACTIVE.get(S.current) || null;
}

function setButtons(peer, state, note) {
  // 明文和密文两个发送按钮都要反映状态：用户不该在等编码时又点另一个。
  for (const id of ["btnSendText", "btnSendCovert"]) {
    const btn = $(id);
    if (!btn) continue;
    if (state === "idle") {
      btn.disabled = false;
      btn.classList.remove("cancel", "busy");
      btn.innerHTML = btn.dataset.label || btn.textContent;
      if (btn.dataset.label) btn.textContent = btn.dataset.label;
      btn.title = btn.dataset.title || "";
    } else {
      if (!btn.dataset.label) {
        btn.dataset.label = btn.textContent;
        btn.dataset.title = btn.title || "";
      }
      btn.disabled = false;                    // 允许点 —— 点了就是取消
      btn.classList.add("cancel");
      btn.textContent = state === "queued" ? "排队中…" : (note || "取消");
      btn.title = "点击取消这条发送";
    }
  }
}

/**
 * 提交并跟踪一个发送任务。
 *
 * @param {object} payload /client/send 的请求体
 * @param {object} opts    {onDone, onFail, onCancel}
 * @returns {Promise<string|null>} 最终状态
 */
export async function submit(payload, opts = {}) {
  const peer = payload.peer;
  if (ACTIVE.has(peer)) {
    toast("这个会话正在发送，请稍候");
    return null;
  }

  let taskId;
  try {
    const r = await api("/client/send", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    taskId = r.task_id;
  } catch (e) {
    toast(e.message);                          // 校验失败（422/409）在这里
    return null;
  }

  // 握手是同步完成的，没有 task。
  if (!taskId) {
    if (opts.onDone) await opts.onDone({});
    return "done";
  }

  ACTIVE.set(peer, { taskId });
  setButtons(peer, "queued");

  const state = await _track(peer, taskId);

  ACTIVE.delete(peer);
  setButtons(peer, "idle");

  if (state === "done") {
    if (opts.onDone) await opts.onDone({});
  } else if (state === "cancelled") {
    toast("已取消发送");
    if (opts.onCancel) await opts.onCancel();
  } else {
    if (opts.onFail) await opts.onFail();
  }
  return state;
}

/** 取消当前会话的任务。 */
export async function cancelCurrent() {
  const active = ACTIVE.get(S.current);
  if (!active) return;
  try {
    await api(`/client/task/${active.taskId}/cancel`, { method: "POST" });
    toast("正在取消…");
  } catch (e) {
    toast(e.message);
  }
}

/**
 * 轮询到任务结束。
 *
 * 用轮询而不是 SSE：连接可能被浏览器掐断，而任务还在后台跑；轮询只丢一次
 * 状态更新，重连后自动恢复。
 */
function _track(peer, taskId) {
  return new Promise((resolve) => {
    const tick = async () => {
      if (!ACTIVE.has(peer)) return resolve("cancelled");
      let task;
      try {
        task = await api(`/client/task/${taskId}`);
      } catch (e) {
        // 404：任务过期或被 gc 掉了。按失败处理，但不能静默。
        ACTIVE.delete(peer);
        return resolve("failed");
      }
      if (task.state === "done") return resolve("done");
      if (task.state === "failed") {
        toast(task.error || "发送失败");
        return resolve("failed");
      }
      if (task.state === "cancelled") return resolve("cancelled");

      setButtons(peer, task.state, task.note);
      setTimeout(tick, 250);
    };
    tick();
  });
}
