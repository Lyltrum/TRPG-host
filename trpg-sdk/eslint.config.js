// ESLint 9 flat config。
//
// SDK 是纯 TypeScript 库，只接入 typescript-eslint 的 recommended 规则集。
// 不用 strictTypeChecked / stylisticTypeChecked ——见 issue #73 决策 3：
// 实测这两套会带来大量存量违规，先用最基础的一档把 lint 立起来。
import tseslint from 'typescript-eslint'

export default tseslint.config(
  {
    // dist 是 rollup 构建产物，不是源码，不需要 lint。
    ignores: ['dist/**'],
  },
  ...tseslint.configs.recommended,
)
