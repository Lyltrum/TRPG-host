import { getAuthToken, sdk, setAuthToken } from './api-client';

export interface AuthResult {
  userId: string;
  token: string;
}

// 注册（account+password，见 ADR-16：登录是玩游戏的硬性前提，不再是可选层）
export async function register(
  account: string,
  password: string,
  nickname: string
): Promise<AuthResult> {
  const res = await sdk.auth.register({ account, password, nickname });
  setAuthToken(res.token);
  return { userId: res.userId, token: res.token };
}

// 登录
export async function login(account: string, password: string): Promise<AuthResult> {
  const res = await sdk.auth.login({ account, password });
  setAuthToken(res.token);
  return { userId: res.userId, token: res.token };
}

// 登出
export async function logout() {
  const token = getAuthToken();
  try {
    if (token) await sdk.auth.logout(token);
  } finally {
    setAuthToken(null);
  }
}

export interface MeResult {
  userId: string;
  account: string;
  nickname: string;
}

// 检查登录状态
export async function fetchMe(): Promise<MeResult | null> {
  const token = getAuthToken();
  if (!token) return null;
  try {
    return await sdk.auth.getMe(token);
  } catch {
    return null;
  }
}

// 修改个人信息（目前只支持改昵称）
export async function updateProfile(nickname: string): Promise<MeResult> {
  const token = getAuthToken();
  if (!token) throw new Error('未登录');
  return sdk.auth.updateNickname({ nickname }, token);
}

/**
 * 改密码。要旧密码，成功后这个账号的**其它**会话失效、当前这条保留
 * ——所以调用方不需要重新登录。
 *
 * ⚠️ 这不是"找回密码"：忘了密码那条路要一个能收验证码的渠道，后端没有。
 */
export async function changePassword(oldPassword: string, newPassword: string): Promise<void> {
  const token = getAuthToken();
  if (!token) throw new Error('未登录');
  await sdk.auth.changePassword({ oldPassword, newPassword }, token);
}

/**
 * 受邀入房用的「只报个名字」注册：账号密码由前端随机生成，玩家只输昵称。
 *
 * 🔴 **不是游客模式**：账号仍然真实存在，`join` 的幂等键（issue #106：掉线
 * 重连认的就是账号）一个字没动。这里省掉的只是**让朋友想一个账号密码**这一步
 * ——聚会时人手一遍注册流程是入房最大的摩擦。
 *
 * 代价写明：账号密码不告诉玩家，token 存在这台设备的 localStorage 里，
 * **换设备或清缓存 = 新身份**（那一局里他会变成一个新玩家）。
 */
export async function registerAsGuest(nickname: string): Promise<AuthResult> {
  const rand = () =>
    Array.from(crypto.getRandomValues(new Uint8Array(9)), (b) => b.toString(36).padStart(2, '0')).join('');
  return register(`g_${rand()}`, rand(), nickname);
}
