import type { ApiClient } from '../client';
import type { Example, ExampleCreateInput, ExampleUpdateInput } from '../types';

/**
 * `/api/v1/examples` 的类型化封装：每个方法对应后端 examples.py 里的一个路由，
 * 调用方（比如 trpg-frontend）不需要自己拼 URL、自己转 JSON。
 *
 * 这是"资源"这个概念的范例——以后新增业务接口（比如房间、角色卡），
 * 照这个文件的结构在 `resources/` 下建一个新的 XxxResource 类，
 * 再在 `index.ts` 的 TrpgSdk 里挂一个新属性就行。
 */
export class ExamplesResource {
  constructor(private readonly client: ApiClient) {}

  list(): Promise<Example[]> {
    return this.client.get<Example[]>('/examples');
  }

  get(id: string): Promise<Example> {
    return this.client.get<Example>(`/examples/${id}`);
  }

  create(payload: ExampleCreateInput): Promise<Example> {
    return this.client.post<Example>('/examples', payload);
  }

  update(id: string, payload: ExampleUpdateInput): Promise<Example> {
    return this.client.put<Example>(`/examples/${id}`, payload);
  }

  /** 命名为 remove 而不是 delete：`delete` 在严格模式下是保留字，
   * 用作方法名容易引起困惑（尤其是配合 `obj.delete()` 这种调用形式）。 */
  remove(id: string): Promise<null> {
    return this.client.delete<null>(`/examples/${id}`);
  }
}
