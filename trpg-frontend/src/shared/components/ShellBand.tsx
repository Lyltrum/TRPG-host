/** 套印色带：外壳每一屏顶部那条斜切的朱红/蓝双版印刷带。
 *
 * 做法是老式双色胶印**没对准**的效果：朱红版在下，蓝版故意错位 6px 压在上面，
 * 再叠一层向下渐隐的半调网点。所以它属于「印刷品」这套语言，跟发光/渐变无关。
 *
 * 🔴 **它只存在于这一个文件里。** 用户明确说过后面可能会删掉这条带子——所以
 * 每一页都只写 `<ShellBand />`，绝不各自拼一遍那三层 div。要撤掉时改这里
 * 一处（或删掉组件让 tsc 把调用点全指出来），不是去 11 个页面里找。
 *
 * 🔴 **它不是装饰，是顶栏底衬**：`slim` 用在内页，高度压到刚好托住返回键与
 * 标题那一行，标题反白压在色带上。这样从登录到内页是同一条带子在变窄，而不是
 * 每页贴一块花纹——后者会跟每屏的顶栏反复打架（这正是选这一档时的最大风险）。
 */
/** 🔴 色带有三档高度，因为**反白文字必须整块落在色带里**。
 *
 * 斜切边是这个材质的灵魂，但它也意味着色带下缘在屏幕两侧差着约 48px——
 * 任何跨过那条斜边的文字都会一半压红底、一半压纸板，两边都读不出来
 * （真机上「AI 智能主持 · 多游戏聚会平台」就是这么消失的）。
 *
 * 所以规矩是：**要么整块在色带里反白，要么整块在纸板上用墨色**，不许骑边。
 * 每一档的高度就是按"这一档要装下多少反白内容"定的：
 *   slim    内页顶栏：返回键 + 一行标题
 *   landing 主页：标志 + 标题 + 副标题
 *   auth    登录/注册：再多一枚标签与两行介绍
 */
const HEIGHTS = {
  slim: '-top-[34px] h-[104px]',
  landing: '-top-10 h-[250px]',
  auth: '-top-10 h-[330px]',
} as const

export type ShellBandVariant = keyof typeof HEIGHTS

export default function ShellBand({ variant = 'landing' }: { variant?: ShellBandVariant }) {
  return (
    <div
      className={`pointer-events-none absolute -left-10 -right-10 overflow-hidden ${HEIGHTS[variant]}`}
      aria-hidden="true"
    >
      <div className="absolute inset-0 bg-rust rotate-[-7deg]" />
      <div className="absolute inset-0 bg-ink-blue opacity-55 mix-blend-multiply rotate-[-7deg] translate-x-[6px] translate-y-[9px]" />
      <div
        className="absolute inset-x-0 top-0 h-[210px] rotate-[-7deg]"
        style={{
          backgroundImage: 'radial-gradient(rgba(36,31,25,.5) 1.2px, transparent 1.3px)',
          backgroundSize: '6px 6px',
          maskImage: 'linear-gradient(to bottom, transparent 52%, #000 100%)',
          WebkitMaskImage: 'linear-gradient(to bottom, transparent 52%, #000 100%)',
        }}
      />
    </div>
  )
}
