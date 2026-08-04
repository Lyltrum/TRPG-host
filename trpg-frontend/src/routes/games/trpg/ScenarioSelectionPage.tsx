import { useEffect, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { BookOpen, Clock, Paperclip, Users, ChevronRight, Upload } from 'lucide-react'
import { getGameById, getScenariosBySystem, SYSTEM_NAMES } from '@/config/games'
import { useGameStore } from '@/stores/game-store'
import { listModules, type ModuleSummary } from '@/services/room'
import Badge from '@/shared/components/Badge'
import ShellPage from '@/shared/components/ShellPage'
import type { Scenario } from '@/types/game'

/** 「8 月 4 日」——导入的模组没有难度也没有简介，日期是它仅有的可显示信息。 */
function formatImportedAt(iso: string): string {
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return ''
  return `${d.getMonth() + 1} 月 ${d.getDate()} 日`
}

// 难度三档：实色描边而不是浅色底——纸板上浅底色块几乎看不出来
const difficultyStyles: Record<string, string> = {
  '入门': 'border-[#4a8a5c] text-[#3a6b46]',
  '进阶': 'border-[#c9822f] text-[#8a5a1e]',
  '挑战': 'border-[#c9452f] text-[#9a2c1c]',
}

export default function ScenarioSelectionPage() {
  const navigate = useNavigate()
  const { gameId, systemId } = useParams<{ gameId: string; systemId: string }>()
  const game = getGameById(gameId || '')
  const scenarios = getScenariosBySystem(systemId || '')

  const setScene = useGameStore((s) => s.setScene)
  const setGame = useGameStore((s) => s.setGame)
  const setReturnFromGameSelect = useGameStore((s) => s.setReturnFromGameSelect)
  const returnFromGameSelect = useGameStore((s) => s.returnFromGameSelect)
  const systemName = SYSTEM_NAMES[systemId || ''] || '未知系统'

  // 🔴 内置模组来自硬编码常量，**导入的模组只有后端知道**——不拉这一趟，
  // 转好的模组永远不会出现在这一屏（`exec/29` 第 5 步）。
  const [imported, setImported] = useState<ModuleSummary[]>([])
  useEffect(() => {
    listModules()
      .then((all) => setImported(all.filter((m) => m.isImported)))
      // 拉不到就只显示内置的：这一屏的主职责是选内置模组，不该被它拖垮。
      .catch(() => setImported([]))
  }, [])

  const selectImported = (module: ModuleSummary) => {
    setScene(module.id, module.title)
    setGame(gameId || '', systemId || '')
    if (returnFromGameSelect) {
      setReturnFromGameSelect(false)
      navigate('/home/create')
    } else {
      navigate('/room/story')
    }
  }

  const handleSelect = (scenario: Scenario) => {
    setScene(scenario.id, scenario.name)
    setGame(gameId || '', systemId || '')
    if (returnFromGameSelect) {
      setReturnFromGameSelect(false)
      navigate('/home/create')
    } else {
      navigate('/room/story')
    }
  }

  return (
    <ShellPage title="选择模组" onBack={() => navigate('/home/create/games')} align="top">
      <p className="text-[11.5px] text-text-muted px-5 pb-3.5">
        {game?.name || '跑团'} · {systemName}
      </p>

      <div className="px-5 flex flex-col gap-3">
        {scenarios.length === 0 && (
          <div className="text-center py-10 text-text-muted text-sm">
            暂无预置模组，您可以自行导入
          </div>
        )}

        {scenarios.map((scenario) => {
          const diffStyle = difficultyStyles[scenario.difficulty] || difficultyStyles['入门']

          return (
            <div
              key={scenario.id}
              onClick={() => handleSelect(scenario)}
              className="press-soft bg-card p-3.5 cursor-pointer active:translate-x-[3px] active:translate-y-[3px] active:shadow-none transition-all duration-100"
            >
              <div className="flex items-start gap-3 mb-2.5">
                <div className="w-11 h-11 flex-shrink-0 flex items-center justify-center border-2 border-text-primary bg-page text-text-primary">
                  <BookOpen className="w-[22px] h-[22px]" strokeWidth={2} />
                </div>
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-2">
                    <h3 className="text-[16px] font-extrabold text-text-primary">{scenario.name}</h3>
                    <span className={`px-1.5 py-[1px] text-[10.5px] font-bold border-2 ${diffStyle}`}>
                      {scenario.difficulty}
                    </span>
                  </div>
                  <p className="text-[11px] text-text-muted mt-0.5 font-mono tracking-[0.03em]">
                    {scenario.nameEn}
                  </p>
                </div>
                <div className="text-text-muted flex-shrink-0 mt-1">
                  <ChevronRight className="w-[18px] h-[18px]" />
                </div>
              </div>
              <p className="text-[11.5px] text-text-muted leading-[1.7] line-clamp-2 mb-2.5">
                {scenario.description}
              </p>
              <div className="flex items-center gap-3.5 text-[10.5px] text-text-muted">
                <span className="flex items-center gap-1">
                  <Users className="w-3.5 h-3.5" />
                  {scenario.playerCount}
                </span>
                <span className="flex items-center gap-1">
                  <Clock className="w-3.5 h-3.5" />
                  {scenario.estimatedTime}
                </span>
                <Badge variant={scenario.status === 'ready' ? 'success' : 'default'}>
                  {scenario.status === 'ready' ? '已就绪' : '开发中'}
                </Badge>
              </div>
            </div>
          )
        })}
      </div>

      {/* 导入的模组。跟内置的区别不是"次一等"，是**能给的信息不一样**：
          它没有人工填的难度与简介，只有导入日期与规模。所以不套同一张卡片，
          用蓝色左脊 + 「我导入的」标区分。 */}
      {imported.length > 0 && (
        <div className="px-5 flex flex-col gap-3 mt-3">
          {imported.map((module) => (
            <div
              key={module.id}
              onClick={() => selectImported(module)}
              className="press-soft bg-card p-3.5 cursor-pointer border-l-[5px] border-l-ink-blue active:translate-x-[3px] active:translate-y-[3px] active:shadow-none transition-all duration-100"
            >
              <div className="flex items-start gap-3">
                <div className="w-11 h-11 flex-shrink-0 flex items-center justify-center border-2 border-text-primary bg-page text-text-primary">
                  <Paperclip className="w-[22px] h-[22px]" strokeWidth={2} />
                </div>
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-2">
                    <h3 className="text-[16px] font-extrabold text-text-primary truncate">
                      {module.title}
                    </h3>
                    <Badge variant="info">我导入的</Badge>
                  </div>
                  <p className="text-[11px] text-text-muted mt-0.5">
                    {module.createdAt ? `${formatImportedAt(module.createdAt)}导入` : '导入的模组'}
                  </p>
                </div>
                <div className="text-text-muted flex-shrink-0 mt-1">
                  <ChevronRight className="w-[18px] h-[18px]" />
                </div>
              </div>
            </div>
          ))}
        </div>
      )}

      {/* 🔴 这里是**跳转**，不是就地上传。
          转换要跑 5–26 分钟，没人会在建房中途等二十分钟——把上传放在这一步，
          等于邀请用户去做一件他在这个上下文里做不完的事。导入是"备料"，
          主入口在主页「我的模组」（`exec/29 §7.2`）。 */}
      <div className="px-5 mt-4">
        <button
          onClick={() => navigate('/home/modules')}
          className="w-full flex items-center justify-center gap-2 py-3 border-2 border-dashed border-text-primary/45 text-text-muted text-[13px] font-semibold active:bg-card transition-all"
        >
          <Upload className="w-[18px] h-[18px]" />
          去导入我自己的模组
        </button>
        <p className="text-[10.5px] text-text-dim text-center mt-2 mb-6">
          转换要十几分钟，建议在开局之前先导好
        </p>
      </div>
    </ShellPage>
  )
}
