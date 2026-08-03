import BrandMark from '@/shared/components/BrandMark'

// /login 和 /login/register 共用的品牌区块——拆成两个独立路由后，这部分
// 视觉是唯一还有必要共享的，不用两边各写一遍。
export default function AuthHeader() {
  return (
    <div className="flex flex-col items-center pt-7 px-5 pb-6 text-center">
      {/* 标志是一枚**压在纸板上的印章**：浅纸片 + 硬描边 + 实心投影，
          跟按钮同一种做法——标志与界面是一套东西，不是贴上去的一张图。 */}
      <div className="press-soft w-[62px] h-[62px] mb-3 bg-card text-text-primary flex items-center justify-center">
        <BrandMark className="w-[60%] h-[60%]" />
      </div>
      {/* 标题反白压在色带上（色带由 ShellPage 铺，这里只管压上去） */}
      <h1 className="text-[22px] font-extrabold text-[#fff5ea] tracking-[0.1em] px-2">
        AI桌游主持人
      </h1>
      <p className="text-[11.5px] text-[#fff5ea]/85 tracking-[0.06em] mt-1">
        AI 智能主持 · 多游戏聚会平台
      </p>
      {/* 🔴 整块都在色带里，所以全部反白——不许有文字骑在斜边上（见 ShellBand）。
          标签在这里用白描边空心，不是纸板色实底：实底会在红底上戳出一个洞。 */}
      <div className="mt-3.5 max-w-[280px]">
        <span className="inline-block text-[11px] px-2.5 py-[3px] mb-2.5 border-2 border-[#fff5ea] text-[#fff5ea] tracking-[0.05em]">
          狼人杀 · 跑团 · 血染钟楼 等
        </span>
        <span className="block text-[11.5px] text-[#fff5ea]/85 leading-[1.8]">
          扫码即玩，AI 担任主持人
          <br />
          与朋友们畅玩各类桌游与聚会游戏
        </span>
      </div>
    </div>
  )
}
