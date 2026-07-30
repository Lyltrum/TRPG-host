/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{js,ts,jsx,tsx}'],
  theme: {
    extend: {
      colors: {
        // 平台默认色（多游戏聚会工具，中性身份，不带任何单一游戏系统的
        // 视觉倾向）——只在"账号/建房/选系统"这类通用页面使用。
        page: '#faf7f0',
        panel: '#f5f0e6',
        card: '#ffffff',
        input: '#fefdfa',
        'border-light': '#e5ded0',
        'border-mid': '#d4cbb8',
        brass: '#b8976a',
        'brass-dark': '#8a6d40',
        'brass-bright': '#ddc190',
        rust: '#c04040',
        'rust-dark': '#7a352c',
        mold: '#4a8a4a',
        'ink-blue': '#4a7098',
        text: {
          primary: '#2c2416',
          body: '#3d3628',
          muted: '#8a8276',
          dim: '#b0a898'
        },
        // "调查员案卷"——COC7 专属主题，只在已确定进入 COC7 世界的页面
        // （建卡向导/游戏内 RoomPage 等）通过限定作用域的方式启用，
        // 不作为全局默认色。方案待定，先保留 token 定义。
        'case-file': '#f1e6cc',
        'case-file-dim': '#e6d8b8',
        'ink-on-file': '#241d12',
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
