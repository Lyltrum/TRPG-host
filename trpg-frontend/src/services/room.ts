import type { CreateRoomResult, ModuleSummary, MyRoomSummary, RoomPreview } from 'trpg-sdk';
import { useRoomStore } from '@/stores/room-store';
import { sdk } from './api-client';

export type { CreateRoomResult, ModuleSummary, MyRoomSummary, RoomPreview };

// 房主/已加入玩家专属的操作（选模组/开始游戏/结束游戏/我的房间列表）需要
// 后端的房间重连凭证（X-Reconnect-Token，issue #39），加入/创建房间时签发、
// 存进 room-store——直接从 store 读，页面组件不需要在每次调用时手动传。
function requireReconnectToken(): string {
  const token = useRoomStore.getState().reconnectToken;
  if (!token) throw new Error('缺少房间重连凭证，请重新加入房间');
  return token;
}

// 创建房间（房主创建即加入，见 §5.2.5）
export async function createGameRoom(
  nickname?: string,
  roomName?: string,
  maxPlayers?: number
): Promise<CreateRoomResult> {
  return sdk.rooms.create({ nickname, roomName: roomName ?? '', maxPlayers: maxPlayers ?? 4 });
}

// 拉取可用模组列表（本次没有做模组导入，只有一款内置模拟模组）
export async function listModules(): Promise<ModuleSummary[]> {
  return sdk.rooms.listModules();
}

// 房主确定模组
export async function selectModule(roomId: string, moduleId: string): Promise<void> {
  await sdk.rooms.selectModule(
    roomId,
    { moduleId, attributeGenMethod: 'point_buy' },
    requireReconnectToken()
  );
}

// 访客用房间码加入（已是本房间玩家则幂等返回已有身份）
export async function joinRoomByCode(
  roomCode: string,
  nickname?: string
): Promise<CreateRoomResult> {
  return sdk.rooms.join(roomCode, { nickname });
}

// 获取房间信息（房间码预览）
export async function getRoomInfo(roomCode: string): Promise<RoomPreview> {
  return sdk.rooms.getInfo(roomCode);
}

// 房主点击「开始游戏」，从大厅推进到背景介绍——访客端轮询这个标记自动跟进
export async function startStory(roomId: string): Promise<void> {
  await sdk.rooms.startStory(roomId, requireReconnectToken());
}

// 我的房间列表——用于「浏览已有游戏」入口。本期后端按重连凭证（而不是账号）
// 识别玩家，只能看到当前会话里加入过的房间，还没建立跨会话的账号级房间历史
// （已知限制）；这里在还没加入过任何房间时直接返回空列表，而不是报错。
export async function listMyRooms(): Promise<MyRoomSummary[]> {
  const token = useRoomStore.getState().reconnectToken;
  if (!token) return [];
  return sdk.rooms.listMyRooms(token);
}

// 房主结束游戏，房间转入「已完成」状态，之后只能查看复盘
export async function endGame(roomId: string): Promise<void> {
  await sdk.rooms.endGame(roomId, requireReconnectToken());
}
