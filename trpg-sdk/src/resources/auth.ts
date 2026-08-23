import type { ApiClient } from '../client';
import type {
  AuthResult,
  ChangePasswordInput,
  LoginInput,
  Me,
  RegisterInput,
  UpdateNicknameInput,
} from '../types';

/**
 * `/api/v1/auth` 的类型化封装：注册/登录/登出/当前用户。
 * 登录凭证走标准的 `Authorization: Bearer <token>` 请求头。
 */
export class AuthResource {
  constructor(private readonly client: ApiClient) {}

  private authenticated(token: string): RequestInit {
    return { headers: { Authorization: `Bearer ${token}` } };
  }

  /** POST /api/v1/auth/register — 注册新账号，成功即登录 */
  register(payload: RegisterInput): Promise<AuthResult> {
    return this.client.post<AuthResult>('/auth/register', payload);
  }

  /** POST /api/v1/auth/login — 账号密码登录 */
  login(payload: LoginInput): Promise<AuthResult> {
    return this.client.post<AuthResult>('/auth/login', payload);
  }

  /** POST /api/v1/auth/logout — 退出登录，使当前 token 失效 */
  logout(token: string): Promise<null> {
    return this.client.post<null>('/auth/logout', null, this.authenticated(token));
  }

  /**
   * POST /api/v1/auth/password — 改密码。
   *
   * 要旧密码（token 可能来自一台没锁屏的机器）。成功后**这个账号的其它会话
   * 全部失效，当前这条保留**——所以调用方不需要重新登录。
   *
   * ⚠️ 这不是"找回密码"：忘了密码那条路要一个能收验证码的渠道，后端没有。
   */
  changePassword(payload: ChangePasswordInput, token: string): Promise<null> {
    return this.client.post<null>('/auth/password', payload, this.authenticated(token));
  }

  /** GET /api/v1/auth/me — 获取当前登录用户，供刷新页面后恢复登录态使用 */
  getMe(token: string): Promise<Me> {
    return this.client.get<Me>('/auth/me', this.authenticated(token));
  }

  /** PATCH /api/v1/auth/me — 修改昵称 */
  updateNickname(payload: UpdateNicknameInput, token: string): Promise<Me> {
    return this.client.patch<Me>('/auth/me', payload, this.authenticated(token));
  }
}
