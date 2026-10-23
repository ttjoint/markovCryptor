// 唯一的界面状态。
//
// 刻意保持很浅：一个当前账号、一个当前会话、两份列表。
// 所有真实数据在服务端内存里，这里只是浏览器侧的视图 ——
// 不要在这里放任何需要持久化的东西。

export const S = {
  uin: "", ready: false,
  mode: "recent",              // recent | friends
  current: null,               // 当前打开的 peer
  conversations: [],           // [{peer,name,unread,established,state_label,last_at}]
  friends: [],                 // [{uin,name}]
};

// 密文区的状态机。放在 store 而不是 covert 视图里，因为 chat 视图也要读它
// （判断能不能发、要不要提示"信道未建立"）。
//   state:    locked | waiting | ready | noimage
//   attempts: 握手重试次数
//   channel:  image | text
export const COVERT = { state: "locked", attempts: 0, channel: "image" };
