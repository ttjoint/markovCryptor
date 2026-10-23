// 左侧列表：最近 / 好友共用同一份渲染代码。
//
// 打开会话的动作由外部注入（setOpener），因为「怎么打开一个会话」属于
// chat 视图的职责。直接 import 会让 list 和 chat 互相依赖，进而变成
// 循环引用 —— ES module 的循环引用不会报错，只会在运行时拿到 undefined。

import { $, clone } from "../core/dom.js";
import { S } from "../core/store.js";
import { avatarFor, setAvatar } from "../core/format.js";

let openPeer = null;

/** 注入「点开一行」的动作。app.js 在装配时调用一次。 */
export function setOpener(fn) {
  openPeer = fn;
}

export function rowFor(item, isConversation) {
  const row = clone("tpl-row");           // 结构见 index.html 的 <template>
  if (!row) return null;

  const peer = isConversation ? item.peer : item.uin;
  row.dataset.peer = peer;
  if (S.current === peer) row.classList.add("active");

  // 好友行也带头像：一眼认人比读名字快。
  // 最近列表的对端不一定是好友，后端已按号码兜底生成了 avatar。
  setAvatar(row.querySelector(".avatar"),
            item.avatar || avatarFor(peer),
            item.name || peer);

  row.querySelector(".name").textContent = item.name || peer;

  const sub = row.querySelector(".sub");
  if (isConversation) {
    sub.textContent = item.established ? "信道已建立" : item.state_label;
  } else {
    sub.remove();                          // 好友行没有副标题
  }

  const mark = row.querySelector(".mark");
  if (isConversation) {
    mark.classList.add(item.established ? "est" : "wait");
    mark.textContent = item.established ? "●" : "○";
    mark.title = item.established ? "信道已建立" : item.state_label;
  } else {
    mark.remove();
  }

  const badge = row.querySelector(".badge");
  if (isConversation && item.unread > 0) {
    badge.textContent = item.unread;
  } else {
    badge.remove();
  }

  row.onclick = () => { if (openPeer) openPeer(peer); };
  return row;
}

export function renderRows() {
  const list = $("list");
  list.innerHTML = "";
  if (S.mode === "recent") {
    if (S.conversations.length === 0) {
      const p = document.createElement("div");
      p.className = "sub";
      p.style.padding = "12px";
      p.textContent = "还没有会话。收到消息或点开好友就会出现。";
      list.appendChild(p);
      return;
    }
    for (const c of S.conversations) list.appendChild(rowFor(c, true));
  } else {
    for (const f of S.friends) list.appendChild(rowFor(f, false));
  }
}

/** 标签页上的总数："最近 (1) / 好友 (29)"。 */
export function renderCounts(data) {
  // 后端给了 counts 就用它（会话数包含尚未渲染的），否则退回数组长度。
  const counts = data.counts || {};
  const nConv = counts.conversations !== undefined
    ? counts.conversations : (data.conversations || []).length;
  const nFriend = counts.friends !== undefined
    ? counts.friends : (data.friends || []).length;
  $("cntRecent").textContent = nConv ? `(${nConv})` : "";
  $("cntFriends").textContent = nFriend ? `(${nFriend})` : "";
}

/** 切换标签页并重绘。 */
export function setMode(mode) {
  S.mode = mode;
  const recent = $("tabRecent");
  const friends = $("tabFriends");
  const isRecent = mode === "recent";
  recent.setAttribute("aria-selected", isRecent ? "true" : "false");
  friends.setAttribute("aria-selected", isRecent ? "false" : "true");
  renderRows();
}
