// 输入框下方的容量条：这条消息会占几张图 / 能不能发。
//
// 数字全部来自后端 `/client/estimate` —— 逐张按待选顺序真实计算，不是平均
// 值。前端算不出来：容量取决于 8×8 块布局、是否放大、放大后的尺寸、握手
// 帧开销。用平均值猜会得到"显示能发、点了却失败"的偏差，那是用户明确要求
// 避免的。
//
// 300ms 防抖：每次按键都请求太浪费，但延迟太久用户会觉得数字跟不上手。

import { $ } from "../core/dom.js";
import { api } from "../core/api.js";
import { COVERT } from "../core/store.js";

const DEBOUNCE_MS = 300;

let timer = null;
let latest = null;          // 最近一次估算结果，发送前要用它判断
let onBlocked = null;       // 超限时禁用发送的回调
//: 请求序号。异步返回是乱序的 —— 用户连打几个字会并发发出多个估算，
//: 先发的可能后回来，用旧结果覆盖新结果会让按钮抽搐（实测 300 字被禁用、
//: 1000 字反而能点）。只认最后一次请求的响应。
let seq = 0;

/** 注册"能不能发"的回调。由 app.js 提供，用于禁用发送按钮。 */
export function setBlocker(fn) {
  onBlocked = fn;
}

/** 最近一次的估算结果（可能为 null）。 */
export function latestEstimate() {
  return latest;
}

/** 立即算一次（切信道、点开会话时用，不防抖）。 */
export async function refresh() {
  const text = $("covertInput")?.value || "";
  const channel = COVERT.channel || "image";

  if (!text.trim()) {
    // 没内容就别问后端，直接清空 —— 空输入时显示"0/9 张"是噪音。
    latest = null;
    _paint(null, channel);
    return null;
  }

  const mine = ++seq;
  try {
    const result = await api("/client/estimate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text, channel }),
    });
    if (mine !== seq) return null;            // 已经有更新的请求，丢弃
    latest = result;
    _paint(result, channel);
    if (onBlocked) onBlocked(!result.ok);
    return result;
  } catch (e) {
    if (mine !== seq) return null;
    // 估算失败不该挡住发送 —— 真正的校验在后端 /client/send 还会做一遍。
    latest = null;
    _paint(null, channel);
    if (onBlocked) onBlocked(false);
    return null;
  }
}

/** 输入变化时调用，防抖。 */
export function schedule() {
  clearTimeout(timer);
  timer = setTimeout(refresh, DEBOUNCE_MS);
}

/** 清空（发送成功后）。 */
export function clear() {
  clearTimeout(timer);
  seq++;                                      // 在途请求的结果作废
  latest = null;
  _paint(null, COVERT.channel || "image");
  if (onBlocked) onBlocked(false);
}

function _paint(result, channel) {
  const main = $("capMain");
  const detail = $("capDetail");
  const warn = $("capWarn");
  if (!main || !detail || !warn) return;

  main.className = "cap-main";
  warn.hidden = true;
  warn.textContent = "";
  detail.textContent = "";

  if (!result) {
    main.textContent = channel === "text" ? "文本信道" : "图片信道";
    detail.textContent = channel === "text"
      ? "极限 2000 字符"
      : "输入内容后显示占用图片数";
    return;
  }

  if (channel === "text") {
    main.textContent = `${result.chars} / ${result.limit} 字符`;
    main.classList.toggle("over", !result.ok);
    main.classList.toggle("ready", result.ok);
    detail.textContent = result.ok ? "文本信道，不占用图片" : "";
    if (!result.ok) _showWarn(warn, result.reason);
    return;
  }

  // 图片信道：主数字就是用户最关心的"占几张"。
  main.textContent = `将会发送 ${result.images_needed}/${result.image_limit} 张`;
  main.classList.toggle("over", !result.ok);
  main.classList.toggle("ready", result.ok && result.images_needed > 0);

  const bits = [`密文 ${result.bytes} 字节`];
  if (result.repeated > 0) {
    bits.push(`其中 ${result.repeated} 张循环重复`);
  }
  if (result.queued > 0 && result.queued < result.image_limit) {
    bits.push(`候选 ${result.queued} 张，循环使用`);
  }
  detail.textContent = bits.join(" · ");

  if (!result.ok) _showWarn(warn, result.reason);
}

function _showWarn(el, reason) {
  if (!reason) return;
  el.hidden = false;
  el.textContent = reason;
}
