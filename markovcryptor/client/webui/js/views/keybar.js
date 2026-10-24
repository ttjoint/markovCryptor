// 密文窗顶部的**连接状态栏**：密钥年龄、指纹、等待/超时、换钥。
//
// ## 为什么全放一条
//
// 「已建立 12 分钟」「等待对方回应 40 秒」「等了 12 分钟，建议重发」
// 描述的是**同一个状态机的不同阶段**。分成三处显示，用户会以为是三件事，
// 而且状态切换时会有东西"跳"到别的位置。
//
// ## 为什么显示而不自动续期
//
// 密钥年龄是真实的安全信号：会话越长，同一套密钥暴露的窗口越大。但自动
// 定期换钥在这里是**反效果**的：
//
//   * 换钥要对方确认（握手是人工确认的），对方离线就卡在 WAITING，
//     反而把一个能用的通道弄成不可用
//   * 换钥不减风险 —— X25519 临时密钥已提供前向保密，旧密钥丢了也解不开
//     旧消息
//   * 何时该换取决于"你现在要发的内容多敏感"，只有用户知道
//
// 同理，等待超时也不自动放弃：对方可能真的晚点回来，决定权在人。

import { $ } from "../core/dom.js";
import { api } from "../core/api.js";

//: 30 分钟起给个提示色。不是告警 —— 长会话不一定是问题。
const AGE_WARN_SECONDS = 30 * 60;

//: 两段式超时的分界点。
//:
//: 30 秒：对方可能只是没看到，说明一下就行。
//: 10 分钟：多半是没收到（QQ 丢包/风控）或人不在，该建议重发了。
const WAIT_HINT_SECONDS = 30;
const WAIT_STALE_SECONDS = 10 * 60;

let info = null;
let ticker = null;
let _syncAt = 0;

export function currentKey() {
  return info;
}

/**
 * 按会话详情里的 handshake 字段刷新。
 * @param {object|null} handshake
 */
export function renderKeyBar(handshake) {
  const bar = $("keyBar");
  if (!bar) return;

  info = handshake || null;
  const established = !!(info && info.established);
  const waiting = !!(info && info.waiting_seconds !== undefined
                     && info.waiting_seconds >= 0 && info.waiting_seconds > 0
                     && !established);
  const pending = !!(info && info.pending);

  bar.hidden = !(established || pending || waiting);
  if (bar.hidden) {
    stopTicker();
    return;
  }

  bar.classList.toggle("waiting", !established);

  _paintKeyBar();
  const fp = $("keyFp");
  if (fp) {
    const shown = (info && info.my_fingerprint) || "";
    fp.textContent = established && shown
      ? `密钥 ${shown.slice(0, 4)}…${shown.slice(-4)}` : "";
    fp.title = shown ? `本端密钥指纹 ${shown}\n可与对方核对` : "";
    fp.hidden = !fp.textContent;
  }

  const btn = $("btnRekey");
  if (btn) {
    // 已建立 → 「换新密钥」；等待中 → 「重新发起」；有待确认请求 → 禁用。
    btn.textContent = established ? "换新密钥" : "重新发起";
    btn.disabled = pending;
    btn.title = pending
      ? "对方已发来请求，先在下方确认"
      : (established ? "与对方重新交换密钥（需要对方确认）"
                     : "再发一次握手请求");
  }
  startTicker();
}

function _paintKeyBar() {
  const icon = $("keyIcon");
  const age = $("keyAge");
  const wait = $("keyWait");
  if (!info) return;

  const established = !!info.established;
  const seconds = established
    ? (info.key_age || 0) + _elapsed()
    : (info.waiting_seconds || 0) + _elapsed();

  if (established) {
    if (icon) icon.textContent = "🔑";
    if (age) {
      age.textContent = `会话已建立 ${durationText(seconds)}`;
      age.classList.toggle("old", seconds >= AGE_WARN_SECONDS);
      age.title = `这是第 ${info.generation || 1} 套密钥。会话越长，`
        + `同一套密钥暴露的窗口越大；觉得敏感就换一次。`;
    }
    if (wait) wait.hidden = true;
    return;
  }

  // ── 未建立：等待对方 ──
  if (icon) icon.textContent = info.rekey ? "🔄" : "⏳";
  if (info.rekey) {
    age.textContent = info.key_age
      ? `请求换钥，等待对方确认（旧密钥仍可用，已用 `
        + `${durationText((info.key_age || 0) + _elapsed())}）`
      : "请求换钥，等待对方确认（旧密钥仍可用）";
  } else {
    age.textContent = seconds >= WAIT_HINT_SECONDS
      ? `已等待对方回应 ${durationText(seconds)}`
      : "已发起握手，等待对方回应";
  }
  age.classList.remove("old");

  if (wait) {
    if (seconds >= WAIT_STALE_SECONDS) {
      wait.hidden = false;
      wait.className = "keywait stale";
      wait.textContent = "对方可能没收到 —— 建议点右侧重新发起";
    } else if (seconds >= WAIT_HINT_SECONDS) {
      wait.hidden = false;
      wait.className = "keywait";
      wait.textContent = "对方可能暂时不在";
    } else {
      wait.hidden = true;
    }
  }
}

function _elapsed() {
  return _syncAt ? (Date.now() - _syncAt) / 1000 : 0;
}

function startTicker() {
  _syncAt = Date.now();
  if (ticker) return;
  ticker = setInterval(_paintKeyBar, 1000);
}

function stopTicker() {
  if (ticker) {
    clearInterval(ticker);
    ticker = null;
  }
  _syncAt = 0;
}

/** "12 秒" / "3 分 20 秒" / "2 小时 5 分" —— 人能一眼理解的粒度。 */
export function durationText(seconds) {
  const s = Math.max(0, Math.floor(seconds));
  if (s < 60) return `${s} 秒`;
  if (s < 3600) {
    const m = Math.floor(s / 60);
    const rest = s % 60;
    return rest ? `${m} 分 ${rest} 秒` : `${m} 分`;
  }
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  return m ? `${h} 小时 ${m} 分` : `${h} 小时`;
}

/** 换钥 / 重新发起：都是发一次握手请求。 */
export async function requestRekey(peer) {
  const btn = $("btnRekey");
  if (btn) btn.disabled = true;
  try {
    await api("/client/send", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ peer, mode: "handshake" }),
    });
    return true;
  } catch (e) {
    if (btn) btn.disabled = false;
    throw e;
  }
}
