// 顶部身份栏：我自己的头像 / 昵称 / QQ 号 / 在线点，外加窗口标题。
//
// 这是界面上唯一回答"我是谁"的地方，所以在线状态宁可显示灰点也不假装：
// 桥不提供状态时 online 是 null，不是 false。

import { $ } from "../core/dom.js";
import { setAvatar } from "../core/format.js";

/**
 * @param {object} data /client/state 的响应
 * @param {object} [data.self] {uin,name,nickname,avatar,online}
 */
export function renderSelf(data) {
  const self = data.self || {};
  const selfName = self.name || self.nickname || data.uin || "";

  // 窗口标题 = "KC - 昵称（QQ号）"。
  // --app 模式把 title 画在窗口左上角，这是唯一显示账号信息的地方；
  // 还没登录完成时只显示 "KC"，不要出现 "KC - undefined（null）"。
  document.title = data.uin ? `KC - ${selfName}（${data.uin}）` : "KC";

  $("meName").textContent = selfName || "正在登录…";
  $("meUin").textContent = data.uin ? ("QQ " + data.uin) : "";
  setAvatar($("meAvatar"), self.avatar, selfName || data.uin);

  // online: true / false / null —— null 表示桥不提供，用灰点而不是假装离线。
  const dot = $("meDot");
  if (!dot) return;
  dot.className = "dot" + (self.online === true ? " online"
                           : self.online === false ? " offline" : "");
  dot.title = self.online === true ? "在线"
            : self.online === false ? "离线"
            : "在线状态未知";
}
