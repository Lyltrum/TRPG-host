// SDK 客户端单例 + 账号会话 token 管理 + 错误信息友好化（issue #66：前端
// 原型接入 trpg-frontend）。原型这里原本手写了一整套 apiRequest/WS 封装
// （外加 mock 模式），现在统一改成调用已经和后端联调过的 trpg-sdk，
// 其余页面组件对这个模块的调用方式（函数名/参数）保持不变。

import { ApiError, createTrpgSdk, type ServerToClientEvent } from 'trpg-sdk';

export { ApiError };

/** 后端端口。前端跑在 9877、后端跑在这个端口，两者同机不同端口。 */
const BACKEND_PORT = 8000;

/**
 * 后端地址：**跟着当前页面的主机名走**，不写死 127.0.0.1。
 *
 * 🔴 局域网开局的关键一行。朋友扫码从 `http://192.168.1.5:9877` 打开时，
 * 写死 127.0.0.1 的话前端会去请求**他自己那台手机**的 8000 端口——邀请链接
 * 做得再对也连不上。同族于 `inviteUrlFor`「域名用当前 host」：**地址要从
 * 运行时的锚点推出来，不要写常量**，这样局域网 IP 换了、以后上公网了都不用
 * 改代码。
 *
 * `VITE_API_BASE_URL` 仍然优先——需要指向另一台机器时（比如后端跑在别处）
 * 用它覆盖。⚠️ 反过来说：`.env` 里把它钉成 127.0.0.1 会让这条派生失效。
 */
function defaultApiBaseUrl(): string {
  const { protocol, hostname, host } = window.location;

  // 🔴 生产构建 = 同源部署：nginx 同时发前端静态文件、反代 /api 与 /ws
  // （见仓库根的 `docker-compose.yml`）。这时后端**没有单独的对外端口**，
  // 拼上 :8000 会打到一个根本没开的端口上。
  //
  // 用 `host`（含端口）而不是 `hostname`：部署在 `example.com:8443` 这种
  // 非标准端口上时，丢掉端口同样连不上。
  //
  // 判据用 Vite 内置的 `PROD` 而不是"端口是不是 9877"之类的猜测——它在
  // `vite build` 时为真、`vite dev` 时为假，跟"哪种部署形态"精确对应。
  if (import.meta.env.PROD) {
    return `${protocol}//${host}/api/v1`;
  }

  return `${protocol}//${hostname}:${BACKEND_PORT}/api/v1`;
}

export const sdk = createTrpgSdk({
  baseUrl: import.meta.env.VITE_API_BASE_URL || defaultApiBaseUrl()
});

const TOKEN_STORAGE_KEY = 'aidm_token';

let authToken: string | null = localStorage.getItem(TOKEN_STORAGE_KEY);

export function setAuthToken(token: string | null) {
  authToken = token;
  if (token) {
    localStorage.setItem(TOKEN_STORAGE_KEY, token);
  } else {
    localStorage.removeItem(TOKEN_STORAGE_KEY);
  }
}

export function getAuthToken(): string | null {
  return authToken;
}

// 把 ApiError/网络错误翻译成用户能看懂的提示——不要把原始异常直接甩给用户。
// trpg-sdk 的 ApiError.message 已经是后端 DTO/业务校验给出的具体原因
// （比如"账号或密码不正确"），直接用就行，不需要再解析响应体。
export function friendlyErrorMessage(err: unknown, fallback = '操作失败，请稍后重试'): string {
  if (err instanceof ApiError) return err.message || fallback;
  if (err instanceof TypeError) return '网络连接失败，请检查网络后重试';
  // 前置校验在请求发出**之前**抛的普通 Error（比如「请先登录」「缺少房间重连
  // 凭证」），它们的 message 本来就是写给用户看的，不该被 fallback 盖掉。
  //
  // 盖掉的后果不只是"信息少了"，而是会主动误导：未登录时点加入房间，用户看到
  // 的是「加入房间失败，请检查房间号」——房间号明明是对的，人会一直去查那个。
  //
  // TypeError 那条要留在前面：它是 fetch 的网络失败，message 是 "Failed to
  // fetch" 这种内部文案，不适合直接展示。
  if (err instanceof Error && err.message) return err.message;
  return fallback;
}

// ── WebSocket：房间级单例连接，跨 Lobby→Room 页面导航保持不断 ──
// 底层是 sdk.roomSocket（issue #60），这里只是保留原型页面组件已经在用
// 的函数签名，避免改动调用方。

export function connectWebSocket(roomId: string): WebSocket {
  return sdk.roomSocket.connect(roomId, authToken ?? '');
}

export function waitForWsOpen(socket: WebSocket): Promise<void> {
  return sdk.roomSocket.waitForOpen(socket);
}

export function onWsMessage(handler: (envelope: ServerToClientEvent) => void): () => void {
  return sdk.roomSocket.onMessage(handler);
}

export function disconnectWebSocket() {
  sdk.roomSocket.disconnect();
}
