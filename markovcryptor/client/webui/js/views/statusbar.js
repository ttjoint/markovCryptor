// 底部状态栏：握手 / 文本发送队列 / 图片发送队列 / 接收积压。
//
// 为什么常驻而不是 toast：用户需要**持续**看到「对方还在发」或「我的图还在
// 编码」，而不是错过一个两秒就消失的提示。这正是原来的问题 —— 大图要编码
// 3 秒，界面上什么都不显示，用户以为卡死了。
//
// 数据来自 /client/state 的 `tasks` 字段，与轮询同频，不额外发请求。

import { $ } from "../core/dom.js";

let lastDropped = 0;

function setText(id, text, busy = false, hidden = false) {
  const el = $(id);
  if (!el) return;
  el.textContent = text;
  el.hidden = hidden;
  el.classList.toggle("busy", busy);
}

/**
 * @param {object} stats {handshakes, text_queued, image_queued, sending,
 *                        inbound_pending, inbound_dropped}
 * @param {object} data 完整的 /client/state，用于模型名等附注
 */
export function render(stats, data) {
  if (!stats) return;

  const handshakes = stats.handshakes || 0;
  setText("sbHandshake", handshakes ? `握手 ${handshakes}` : "握手 0",
          handshakes > 0);

  // 发送：排队数 + 进行中。"1 排队"比单纯一个数字更能说明"没卡死，是在等"。
  const textQueued = stats.text_queued || 0;
  const imageQueued = stats.image_queued || 0;
  const sending = stats.sending || 0;

  setText("sbText", textQueued ? `文本 排队 ${textQueued}` : "文本 空闲",
          textQueued > 0);
  setText("sbImage",
          imageQueued ? `图片 排队 ${imageQueued}` : "图片 空闲",
          imageQueued > 0);

  const inbound = stats.inbound_pending || 0;
  setText("sbInbound", inbound ? `收 ${inbound}` : "收 0", inbound > 0);

  // 丢弃计数是累计值，只在增长时提示，避免一直挂着吓人。
  const dropped = stats.inbound_dropped || 0;
  if (dropped > lastDropped) {
    setText("sbDropped",
            `对方发送过快，已跳过 ${dropped} 条`, true, false);
    lastDropped = dropped;
  } else if (dropped === 0) {
    lastDropped = 0;
    setText("sbDropped", "", false, true);
  }

  // 右下角：当前信道能力。没有文本模型时明确说出来，免得用户以为能发。
  const model = data && data.models;
  if (model && model.total) {
    setText("sbModel", `模型 ${model.loaded}/${model.total}`);
  } else {
    setText("sbModel", "仅图片信道");
  }

  // 有东西在跑时给整条状态栏一个标记，方便以后加动画。
  const busyEl = $("statusbar");
  if (busyEl) busyEl.classList.toggle("busy", sending > 0 || handshakes > 0);
}
