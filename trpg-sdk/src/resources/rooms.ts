import type { ApiClient } from '../client';
import type {
  CreateRoomInput,
  CreateRoomResult,
  ModuleSummary,
  SelectModuleInput,
  JoinRoomInput,
  RoomPreview,
  MyRoomSummary,
} from '../types';

/**
 * `/api/v1/rooms` 和 `/api/v1/modules` 的类型化封装。
 */
export class RoomsResource {
  constructor(private readonly client: ApiClient) {}

  /** POST /api/v1/rooms — 创建房间，返回 roomId/roomCode/reconnectToken/playerId */
  create(payload: CreateRoomInput): Promise<CreateRoomResult> {
    return this.client.post<CreateRoomResult>('/rooms', payload);
  }

  /** GET /api/v1/modules — 获取可用模组列表 */
  listModules(): Promise<ModuleSummary[]> {
    return this.client.get<ModuleSummary[]>('/modules');
  }

  /** POST /api/v1/rooms/{roomId}/module — 房主选定模组 */
  selectModule(roomId: string, payload: SelectModuleInput): Promise<null> {
    return this.client.post<null>(`/rooms/${roomId}/module`, payload);
  }

  /** POST /api/v1/rooms/{roomCode}/join — 用房间码加入房间 */
  join(roomCode: string, payload: JoinRoomInput): Promise<CreateRoomResult> {
    return this.client.post<CreateRoomResult>(`/rooms/${roomCode}/join`, payload);
  }

  /** GET /api/v1/rooms/{roomCode} — 获取房间信息 + 玩家列表 */
  getInfo(roomCode: string): Promise<RoomPreview> {
    return this.client.get<RoomPreview>(`/rooms/${roomCode}`);
  }

  /** POST /api/v1/rooms/{roomId}/start-story — 房主开始游戏 */
  startStory(roomId: string): Promise<null> {
    return this.client.post<null>(`/rooms/${roomId}/start-story`, null);
  }

  /** GET /api/v1/me/rooms — 获取我的房间列表 */
  listMyRooms(): Promise<MyRoomSummary[]> {
    return this.client.get<MyRoomSummary[]>('/me/rooms');
  }

  /** POST /api/v1/rooms/{roomId}/end — 房主结束游戏 */
  endGame(roomId: string): Promise<null> {
    return this.client.post<null>(`/rooms/${roomId}/end`, null);
  }
}
