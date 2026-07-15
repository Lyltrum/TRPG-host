/**
 * trpg-sdk 的公开入口。打包后 `trpg-frontend` 通过 `import { createTrpgSdk } from
 * 'trpg-sdk'` 使用，实际解析到的是 dist/ 下 rollup 打出来的产物（见 rollup.config.mjs）。
 */

import { ApiClient, type ApiClientOptions } from './client';
import { ExamplesResource } from './resources/examples';

export * from './types';
export { ApiClient, ApiError } from './client';
export type { ApiClientOptions } from './client';

/**
 * SDK 的顶层门面：每种业务资源（目前只有 examples）挂一个只读属性，
 * 调用方用 `sdk.examples.list()` 这样的形式访问，不需要分别 new 各个 Resource。
 */
export class TrpgSdk {
  readonly examples: ExamplesResource;

  constructor(options: ApiClientOptions) {
    const client = new ApiClient(options);
    this.examples = new ExamplesResource(client);
  }
}

/** 工厂函数，比直接 `new TrpgSdk(...)` 更符合大多数 JS SDK 的惯用写法。 */
export function createTrpgSdk(options: ApiClientOptions): TrpgSdk {
  return new TrpgSdk(options);
}
