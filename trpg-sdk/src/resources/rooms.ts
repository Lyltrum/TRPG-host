import type { ApiClient } from '../client';
import type {
  ChatMessage,
  CreateRoomInput,
  CreateRoomResult,
  ModuleSummary,
  SelectModuleInput,
  JoinRoomInput,
  RoomPreview,
  MyRoomSummary,
  RoomSummary,
  ReplayEvent,
  PartyCharacter,
  RoomPlayerSummary,
  AddAiPlayerInput,
} from '../types';

/**
 * `/api/v1/rooms` 和 `/api/v1/modules` 的类型化封装。
 */
export class RoomsResource {
  constructor(private readonly client: ApiClient) {}

  /**
   * 房间身份：`X-Reconnect-Token`，代表"你是这个房间里的哪个玩家"。
   *
   * 跟下面的 `accountAuth` 是两套**不同的**凭证，别搞混——这个类以前只有一个
   * 叫 `authenticated` 的方法，issue #106 给创建/加入房间接上账号凭证之后，
   * 同一个类里同时出现两种凭证，含糊的名字很容易让人传错一个进去，而传错的
   * 后果是 401，排查时又容易怀疑到 token 本身失效。
   */
  private roomAuth(reconnectToken: string): RequestInit {
    return { headers: { 'X-Reconnect-Token': reconnectToken } };
  }

  /** 账号身份：`Authorization: Bearer`，代表"你是哪个用户"。 */
  private accountAuth(token: string): RequestInit {
    return { headers: { Authorization: `Bearer ${token}` } };
  }

  /**
   * POST /api/v1/rooms — 创建房间，返回 roomId/roomCode/reconnectToken/playerId
   *
   * issue #106 起需要账号 token：房间和房主玩家都要关联到真实账号，否则
   * `hostUserId`/`userId` 永远是空的，「我的游戏」和跨设备找回都无从谈起。
   */
  create(payload: CreateRoomInput, token: string): Promise<CreateRoomResult> {
    return this.client.post<CreateRoomResult>('/rooms', payload, this.accountAuth(token));
  }

  /** GET /api/v1/modules — 获取可用模组列表 */
  listModules(token?: string): Promise<ModuleSummary[]> {
    // 🔴 token 是**可选**的，但不传就只看得到内置模组——自己导入的模组归自己
    // 所有，服务端认不出你是谁就不会给（`exec/29`）。这个接口在导入功能之前
    // 完全公开，所以不能改成必需，否则当场打断既有调用方。
    return this.client.get<ModuleSummary[]>(
      '/modules',
      token ? { headers: { Authorization: `Bearer ${token}` } } : undefined
    );
  }

  /** POST /api/v1/rooms/{roomId}/module — 房主选定模组 */
  selectModule(
    roomId: string,
    payload: SelectModuleInput,
    reconnectToken: string
  ): Promise<null> {
    return this.client.post<null>(
      `/rooms/${roomId}/module`,
      payload,
      this.roomAuth(reconnectToken)
    );
  }

  /**
   * POST /api/v1/rooms/{roomCode}/join — 用房间码加入房间
   *
   * issue #106 起需要账号 token，且**已是房间成员时幂等返回既有身份**——所以
   * 这个方法同时承担"加入"和"重连"两个用途，掉线/换设备后直接再调一次即可。
   */
  join(roomCode: string, payload: JoinRoomInput, token: string): Promise<CreateRoomResult> {
    return this.client.post<CreateRoomResult>(
      `/rooms/${roomCode}/join`,
      payload,
      this.accountAuth(token)
    );
  }

  /** GET /api/v1/rooms/{roomCode} — 获取房间信息 + 玩家列表 */
  getInfo(roomCode: string): Promise<RoomPreview> {
    return this.client.get<RoomPreview>(`/rooms/${roomCode}`);
  }

  /**
   * POST /api/v1/rooms/{roomId}/ai-players — 房主加一个 AI 队友（exec/21）
   *
   * 人不齐时补位。返回的成员带 `isAi`，且它落座就是 `ready`/`hasCharacter`
   * 状态（它没有连接，点不了「已就绪」，也不走建卡向导）。只能在开局前加。
   */
  addAiPlayer(
    roomId: string,
    reconnectToken: string,
    payload?: AddAiPlayerInput
  ): Promise<RoomPlayerSummary> {
    return this.client.post<RoomPlayerSummary>(
      `/rooms/${roomId}/ai-players`,
      payload ?? {},
      this.roomAuth(reconnectToken)
    );
  }

  /** POST /api/v1/rooms/{roomId}/start-story — 房主开始游戏 */
  startStory(roomId: string, reconnectToken: string): Promise<null> {
    return this.client.post<null>(
      `/rooms/${roomId}/start-story`,
      null,
      this.roomAuth(reconnectToken)
    );
  }

  /**
   * GET /api/v1/me/rooms — 获取我的房间列表
   *
   * issue #106 起凭证从房间的 `X-Reconnect-Token` 换成账号 token，返回该账号的
   * **全部**房间。原来按重连凭证查，一个凭证只对应一个房间，这个列表实际上是
   * 「这个浏览器的最后一个房间」。
   */
  listMyRooms(token: string): Promise<MyRoomSummary[]> {
    return this.client.get<MyRoomSummary[]>('/me/rooms', this.accountAuth(token));
  }

  /** POST /api/v1/rooms/{roomId}/end — 房主结束游戏 */
  endGame(roomId: string, reconnectToken: string): Promise<null> {
    return this.client.post<null>(
      `/rooms/${roomId}/end`,
      null,
      this.roomAuth(reconnectToken)
    );
  }

  /** GET /api/v1/rooms/{roomId}/summary — 复盘摘要（issue #77 新增，本期未实现） */
  getSummary(roomId: string, reconnectToken: string): Promise<RoomSummary> {
    return this.client.get<RoomSummary>(
      `/rooms/${roomId}/summary`,
      this.roomAuth(reconnectToken)
    );
  }

  /** GET /api/v1/rooms/{roomId}/replay — 逐条事件回放（issue #77 新增） */
  getReplay(roomId: string, reconnectToken: string): Promise<ReplayEvent[]> {
    return this.client.get<ReplayEvent[]>(
      `/rooms/${roomId}/replay`,
      this.roomAuth(reconnectToken)
    );
  }

  /**
   * GET /api/v1/rooms/{roomId}/characters — 队伍里每个人的角色卡（exec/14 P5.3）
   *
   * 跟「读回自己那张」（`getCharacter`）是两个接口：这个只要求你是房间成员，
   * 返回房间内**全部**玩家（含自己、含还没建卡的，后者 `status` 为 `absent`）。
   * 真人桌上角色卡互相传阅，⑦检定与⑧HP/SAN 已裁决为公开，故不做脱敏。
   */
  listPartyCharacters(roomId: string, reconnectToken: string): Promise<PartyCharacter[]> {
    return this.client.get<PartyCharacter[]>(
      `/rooms/${roomId}/characters`,
      this.roomAuth(reconnectToken)
    );
  }

  /**
   * GET /api/v1/rooms/{roomId}/messages — 讨论区历史消息，倒序分页（issue #107）
   *
   * 刷新页面/断线重连后靠它拉回聊天历史（实时消息走 WS 的 chat.message 广播）。
   * `before` 传上一页最后一条的 messageId 继续往前翻；返回最新在前，渲染时
   * 由前端自行反转成时间正序。仅本房间成员可查（房间凭证）。
   */
  listMessages(
    roomId: string,
    reconnectToken: string,
    options?: { before?: string; limit?: number }
  ): Promise<ChatMessage[]> {
    const params = new URLSearchParams();
    if (options?.before) params.set('before', options.before);
    if (options?.limit !== undefined) params.set('limit', String(options.limit));
    const query = params.size > 0 ? `?${params.toString()}` : '';
    return this.client.get<ChatMessage[]>(
      `/rooms/${roomId}/messages${query}`,
      this.roomAuth(reconnectToken)
    );
  }
}
