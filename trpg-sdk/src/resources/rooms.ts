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

  /**
   * DELETE /api/v1/rooms/{roomId}/players/{playerId} — 把某个人移出房间
   *
   * 房主踢别人，或者本人自己退出（大厅的「离开房间」走的就是这条）。
   *
   * 🔴 **只在大厅阶段**。对局中踢人要连带处理他的位置、待掷队列里挂着的骰子、
   * 分组、正在等他确认的会合——而"开局之后想把人赶走"是社交问题，不是软件问题。
   */
  kickPlayer(roomId: string, playerId: string, reconnectToken: string): Promise<null> {
    return this.client.delete<null>(
      `/rooms/${roomId}/players/${playerId}`,
      this.roomAuth(reconnectToken)
    );
  }

  /**
   * POST /api/v1/rooms/{roomId}/host — 转让房主
   *
   * 不限阶段：真实场景恰恰是**开局之后**房主要先走。不能转给 AI 队友
   * （它拿不到重连凭证，也永远不会去点开始游戏）。
   */
  transferHost(roomId: string, playerId: string, reconnectToken: string): Promise<null> {
    return this.client.post<null>(
      `/rooms/${roomId}/host`,
      { playerId },
      this.roomAuth(reconnectToken)
    );
  }

  /**
   * PATCH /api/v1/rooms/{roomId} — 改人数上限
   *
   * 下界是**当前人数**，不是 1：调到比在座的人还少，等于让已经在玩的人凭空
   * 超员，而没有任何地方会去踢掉多出来的。
   */
  updateSettings(roomId: string, maxPlayers: number, reconnectToken: string): Promise<null> {
    return this.client.patch<null>(
      `/rooms/${roomId}`,
      { maxPlayers },
      this.roomAuth(reconnectToken)
    );
  }

  /**
   * POST /api/v1/rooms/{roomId}/disband — 房主解散房间
   *
   * 跟 `endGame` 的区别只有阶段条件：那条要求进行中（"把这局收掉"），
   * 这条允许任何还没结束的阶段（"人没凑齐，散了"）。**都不删数据**，
   * 回放照常打得开。
   */
  disband(roomId: string, reconnectToken: string): Promise<null> {
    return this.client.post<null>(`/rooms/${roomId}/disband`, null, this.roomAuth(reconnectToken));
  }

  /**
   * DELETE /api/v1/rooms/{roomId} — 房主彻底删除房间
   *
   * 🔴 跟 `disband` 是两件事：那条只标已完成、复盘照常打得开；这条把房间、事件
   * 流、角色卡、复盘一起删掉，**不可撤回**。走账号鉴权（入口在「我的房间」，
   * 那里没有房间的重连凭证）。
   */
  remove(roomId: string, token: string): Promise<null> {
    return this.client.delete<null>(`/rooms/${roomId}`, {
      headers: { Authorization: `Bearer ${token}` },
    });
  }

  /**
   * POST /api/v1/rooms/{roomId}/players/{playerId}/away — 中途离开 / 回来
   *
   * `away=true` 让这个角色暂时退出剧情：他**不进守秘人的在场名单**（那一半是
   * 硬的），守秘人下一段会给一个说得通的理由把他送出这一幕（那一半是概率的）。
   * 本人或房主可操作。
   */
  setPlayerAway(
    roomId: string,
    playerId: string,
    away: boolean,
    reconnectToken: string
  ): Promise<null> {
    return this.client.post<null>(
      `/rooms/${roomId}/players/${playerId}/away`,
      { away },
      this.roomAuth(reconnectToken)
    );
  }

  /** POST /api/v1/rooms/{roomId}/end — 房主结束游戏 */
  endGame(roomId: string, reconnectToken: string): Promise<null> {
    return this.client.post<null>(
      `/rooms/${roomId}/end`,
      null,
      this.roomAuth(reconnectToken)
    );
  }

  /**
   * GET /api/v1/rooms/{roomId}/summary — 复盘摘要
   *
   * 上半 `highlights` 是代码算的数字（时长/掷骰成败/SAN/线索），下半
   * `summaryText` 是模型写的一段回顾——**没配 key 时它是 null**，那是如实的
   * 降级，不是失败。
   */
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
