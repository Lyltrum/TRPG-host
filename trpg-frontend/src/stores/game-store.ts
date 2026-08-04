import { create } from 'zustand'

export type GamePhase = 'lobby' | 'playing' | 'paused' | 'ended'

interface GameState {
  gameId: string | null
  systemId: string | null
  sceneId: string | null
  /**
   * 选中模组的显示名。
   *
   * 🔴 **不能靠 `getScenarioById(sceneId)` 反查**：那张表是硬编码常量，
   * 只认内置模组。导入的模组查不到，创建房间页会显示成「模组：」后面一片空白
   * （`exec/29`）。选的人本来就知道名字，选的时候一起存下来。
   *
   * 允许为 `null`：访客从房间预览回填时，`moduleTitle` 本身就可能是空的。
   * **"不知道名字"要能表达出来**，不能用 id 或空串冒充。
   */
  sceneName: string | null
  phase: GamePhase
  returnFromGameSelect: boolean
  setGame: (gameId: string, systemId: string) => void
  setScene: (sceneId: string, sceneName: string | null) => void
  setPhase: (phase: GamePhase) => void
  setReturnFromGameSelect: (v: boolean) => void
  reset: () => void
}

export const useGameStore = create<GameState>((set) => ({
  gameId: null,
  systemId: null,
  sceneId: null,
  sceneName: null,
  phase: 'lobby',
  returnFromGameSelect: false,
  setGame: (gameId, systemId) => set({ gameId, systemId }),
  setScene: (sceneId, sceneName) => set({ sceneId, sceneName }),
  setPhase: (phase) => set({ phase }),
  setReturnFromGameSelect: (v) => set({ returnFromGameSelect: v }),
  reset: () =>
    set({
      gameId: null,
      systemId: null,
      sceneId: null,
      sceneName: null,
      phase: 'lobby',
      returnFromGameSelect: false,
    }),
}))
