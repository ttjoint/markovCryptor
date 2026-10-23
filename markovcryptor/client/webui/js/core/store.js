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
