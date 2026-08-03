/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{js,ts,jsx,tsx}'],
  theme: {
    extend: {
      colors: {
        // 🔴 语义 token 全部走 CSS 变量，**为了能一页一页地换皮**。
        //
        // 默认值（`:root`，见 styles.css）仍是平台中性色；`.theme-coc` 作用域内
        // 换成「卷宗」暗色。这样改造 RoomPage 时其余 23 个页面一动不动——
        // 上一轮教训：直接改 token 字面量等于全站同时变成"暗但没做过"的半成品。
        //
        // 三段式 `rgb(... / <alpha-value>)` 是为了 `bg-page/60` 这类透明度写法
        // 仍然可用；变量里存的是空格分隔的 R G B，不是 hex。
        page: 'rgb(var(--c-page) / <alpha-value>)',
        panel: 'rgb(var(--c-panel) / <alpha-value>)',
        card: 'rgb(var(--c-card) / <alpha-value>)',
        input: 'rgb(var(--c-input) / <alpha-value>)',
        'border-light': 'rgb(var(--c-border-light) / <alpha-value>)',
        'border-mid': 'rgb(var(--c-border-mid) / <alpha-value>)',
        brass: 'rgb(var(--c-brass) / <alpha-value>)',
        'brass-dark': 'rgb(var(--c-brass-dark) / <alpha-value>)',
        'brass-bright': 'rgb(var(--c-brass-bright) / <alpha-value>)',
        rust: 'rgb(var(--c-rust) / <alpha-value>)',
        'rust-dark': 'rgb(var(--c-rust-dark) / <alpha-value>)',
        mold: 'rgb(var(--c-mold) / <alpha-value>)',
        'ink-blue': 'rgb(var(--c-ink-blue) / <alpha-value>)',
        text: {
          primary: 'rgb(var(--c-text-primary) / <alpha-value>)',
          body: 'rgb(var(--c-text-body) / <alpha-value>)',
          muted: 'rgb(var(--c-text-muted) / <alpha-value>)',
          dim: 'rgb(var(--c-text-dim) / <alpha-value>)'
        },

        // ── 卷宗方向的三种纸。不随主题变，它们**就是**材质本身 ──
        // 守秘人叙事 = 书页；玩家发言 = 便签；面板 = 档案（牛皮纸）。
        book: '#e6dcc4',
        'memo-self': '#d3cdb0', // 自己：暖一档
        'memo-mate': '#b6b8a8', // 队友：冷一档
        dossier: '#cbb894',
        ink: '#241d14', // 落在纸上的墨
        // 🔴 纸上的次要文字。原值 #5a4c39 压在牛皮纸（#cbb894）上只有 4.28:1，
        // 卡在「次要 ≥4.5:1」判据下面——真机上小字发灰读不出来。加深到 5.5:1。
        'ink-soft': '#4a3c2c',
        // 纸上最弱的一档（禁用态、占位）。**实色**，不是把 ink-soft 调透明度：
        // 稀释后明度差会被压到 2:1 上下，太阳底下直接消失。这一档 ≈3.4:1。
        'ink-faint': '#77664e',
        // 兼容旧名（建卡向导等页面可能引用）
        'case-file': '#e6dcc4',
        'case-file-dim': '#cbb894',
        'ink-on-file': '#241d14',
        'ink-on-file-body': '#3a2f1c'
      },
      borderRadius: {
        sm: '6px',
        md: '10px'
      },
      fontFamily: {
        sans: [
          '-apple-system',
          'BlinkMacSystemFont',
          '"PingFang SC"',
          '"Microsoft YaHei"',
          '"Helvetica Neue"',
          'sans-serif'
        ],
        mono: ['"Courier New"', '"SF Mono"', 'monospace'],
        // 标题/模组名/角色名专用衬线字体——评一份1920年代印刷品的调性；
        // 正文对话继续用 sans，长文本不能牺牲阅读效率。
        display: ['"Songti SC"', '"Noto Serif SC"', '"STSong"', 'Georgia', 'serif']
      },
      maxWidth: {
        phone: '390px'
      }
    }
  },
  plugins: []
};
