import { useNavigate, useParams } from 'react-router-dom'
import { BookOpen, Clock, Users, ChevronRight, Upload } from 'lucide-react'
import { getGameById, getScenariosBySystem, SYSTEM_COLORS } from '@/config/games'
import { useGameStore } from '@/stores/game-store'
import Badge from '@/shared/components/Badge'
import ShellPage from '@/shared/components/ShellPage'
import type { Scenario } from '@/types/game'

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
  const colors = SYSTEM_COLORS[systemId || '']
  const systemName = colors?.name || '未知系统'

  const handleSelect = (scenario: Scenario) => {
    setScene(scenario.id)
    setGame(gameId || '', systemId || '')
    if (returnFromGameSelect) {
      setReturnFromGameSelect(false)
      navigate('/home/create')
    } else {
      navigate('/room/story')
    }
  }

  return (
    <ShellPage title="选择模组" onBack={() => navigate('/home/create/games')} contentClassName="justify-start">
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

      {/* 自行导入模组 */}
      <div className="px-5 mt-4">
        <button
          onClick={() => {
            /* TODO: 导入模组的弹窗或页面 */
          }}
          className="w-full flex items-center justify-center gap-2 py-3 border-2 border-dashed border-text-primary/45 text-text-muted text-[13px] font-semibold active:bg-card transition-all"
        >
          <Upload className="w-[18px] h-[18px]" />
          自行导入模组
        </button>
        <p className="text-[10.5px] text-text-dim text-center mt-2 mb-6">
          支持 JSON / YAML 格式的模组文件
        </p>
      </div>
    </ShellPage>
  )
}
