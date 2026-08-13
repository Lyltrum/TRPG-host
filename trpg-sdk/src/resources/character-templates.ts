import type { ApiClient } from '../client';
import type {
  CharacterTemplate,
  SaveCharacterTemplateInput,
  UpdateCharacterTemplateInput,
} from '../types';

/**
 * `/api/v1/me/character-templates` 的类型化封装——玩家的「我的常用角色卡」库
 * （issue #77 决策 5，本期后端是 NOT_IMPLEMENTED 桩）。跨房间复用，属于账号
 * 级资源，走 `Authorization: Bearer <token>` 鉴权（不是房间的重连凭证）。
 */
export class CharacterTemplatesResource {
  constructor(private readonly client: ApiClient) {}

  private authenticated(token: string): RequestInit {
    return { headers: { Authorization: `Bearer ${token}` } };
  }

  /** GET /api/v1/me/character-templates — 我的卡库列表 */
  list(token: string): Promise<CharacterTemplate[]> {
    return this.client.get<CharacterTemplate[]>(
      '/me/character-templates',
      this.authenticated(token)
    );
  }

  /** POST /api/v1/me/character-templates — 把一张角色卡保存为常用卡 */
  save(payload: SaveCharacterTemplateInput, token: string): Promise<CharacterTemplate> {
    return this.client.post<CharacterTemplate>(
      '/me/character-templates',
      payload,
      this.authenticated(token)
    );
  }

  /** GET /api/v1/me/character-templates/{templateId} — 卡库详情 */
  get(templateId: string, token: string): Promise<CharacterTemplate> {
    return this.client.get<CharacterTemplate>(
      `/me/character-templates/${templateId}`,
      this.authenticated(token)
    );
  }

  /**
   * PATCH /api/v1/me/character-templates/{templateId} — 改卡库里那张卡的文字
   *
   * 只收文字类字段（卡名 + 姓名/性别/居住地/出生地/背景）。属性、年龄、职业、
   * 技能这些规则数后端会**显式拒绝**，不是静默丢弃。
   */
  update(
    templateId: string,
    payload: UpdateCharacterTemplateInput,
    token: string
  ): Promise<CharacterTemplate> {
    return this.client.patch<CharacterTemplate>(
      `/me/character-templates/${templateId}`,
      payload,
      this.authenticated(token)
    );
  }

  /** DELETE /api/v1/me/character-templates/{templateId} — 删除常用卡 */
  remove(templateId: string, token: string): Promise<null> {
    return this.client.delete<null>(
      `/me/character-templates/${templateId}`,
      this.authenticated(token)
    );
  }
}
