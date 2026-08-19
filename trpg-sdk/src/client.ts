import type { ApiResponse } from './types';

/**
 * 后端返回 `{ success: false, error: {...} }` 时，统一转成这个异常抛出，
 * 而不是让调用方每次都手动检查 `response.success`——用 try/catch 处理错误
 * 更符合 JS/TS 里常见的错误处理习惯，也方便和网络层面的异常（比如断网）
 * 用同一套 catch 逻辑处理。
 */
export class ApiError extends Error {
  readonly code: string;
  readonly status: number;
  /**
   * 结构化错误详情（后端 `AppException.details`）。
   *
   * 🔴 后端**一直在发**这一段（建卡校验的每条 issue 都带 code/field/message），
   * 而这个类此前只收 code/message —— 调用方要拿"是哪个字段出错"只能去正则
   * 解析拼好的那句话。「整条链都在，就是没人能用到」的又一处（2026-08-19
   * 给装备申辩接线时发现）。
   */
  readonly details: Array<Record<string, string>>;

  constructor(
    code: string,
    message: string,
    status: number,
    details: Array<Record<string, string>> = []
  ) {
    super(message);
    this.name = 'ApiError';
    this.code = code;
    this.status = status;
    this.details = details;
  }
}

/** `request` 的内部开关。不进公开 API——调用方走 `postForm` 就够了。 */
interface RequestExtras {
  /** 让运行时自己决定 Content-Type（multipart 必须这样，见 `request`）。 */
  omitContentType?: boolean;
}

export interface ApiClientOptions {
  /** 后端 API 的根地址，比如 "http://127.0.0.1:8000/api/v1"（要包含版本前缀）。 */
  baseUrl: string;
  /** 自定义 fetch 实现，主要给 Node 环境或单元测试注入 mock 用；不传就用全局 fetch。 */
  fetch?: typeof fetch;
}

/**
 * 最底层的 HTTP 封装：拼 URL、加公共 header、解析统一响应信封、
 * 把 `success:false` 转成 ApiError。上层的 `resources/*`（比如 AuthResource）
 * 都是基于这个类的 get/post/put/delete 方法实现的，不直接碰 fetch。
 */
export class ApiClient {
  private readonly baseUrl: string;
  private readonly fetchImpl: typeof fetch;

  constructor(options: ApiClientOptions) {
    // 去掉末尾的斜杠，避免使用方传了 "http://host/" 导致后面拼接时出现 "//"。
    this.baseUrl = options.baseUrl.replace(/\/$/, '');
    // 注意这里必须 `.bind(globalThis)`：如果直接写 `options.fetch ?? fetch`，
    // 拿到的是一个和 `window`/`globalThis` 解绑的裸函数引用，之后用
    // `this.fetchImpl(...)` 的方式调用会报 `Illegal invocation`
    // ——浏览器原生 fetch 的实现依赖调用时的 this 是 window，这是我们在
    // 真机联调时踩到的一个真实 bug，这里的注释就是防止以后又被坑一次。
    this.fetchImpl = options.fetch ?? fetch.bind(globalThis);
  }

  /**
   * 发起一次请求并按统一响应信封解析结果。
   * 成功时直接返回 `data` 字段（调用方不需要自己拆 `{success,data,error}`）；
   * 失败（`success:false`）或网络异常都会以抛异常的形式表现。
   */
  async request<T>(path: string, init?: RequestInit, options?: RequestExtras): Promise<T> {
    // `HeadersInit` 有三种形态：Headers 实例 / string[][] / Record<string,string>。
    // 之前这里写的 `{...init?.headers}` 只对 Record<string,string> 是对的——
    // 展开 Headers 实例得到 `{}`（它没有可枚举自有属性），展开 string[][]
    // 得到 `{0:[...],1:[...]}`，两种情况下调用方传的 header 都会静默失效、
    // 不报错（issue #75 code review 时发现的真实 bug，见 client.test.ts）。
    // `new Headers(...)` 本身就能正确解析这三种形态，这里委托给它，而不是
    // 自己再判断一次调用方传的是哪种形态。
    // 🔴 multipart 一定不能自己写 Content-Type：那个头必须带 `boundary=...`，
    // 而 boundary 是运行时序列化 FormData 时才生成的。写死成 json（甚至写死成
    // multipart/form-data 不带 boundary）都会让后端收到一个解析不了的 body，
    // 而且**报错发生在服务端**——本地看起来只是"上传失败"，很难查。
    const headers = new Headers(
      options?.omitContentType ? {} : { 'Content-Type': 'application/json' }
    );
    if (init?.headers) {
      new Headers(init.headers).forEach((value, key) => headers.set(key, value));
    }

    const response = await this.fetchImpl(`${this.baseUrl}${path}`, {
      ...init,
      headers
    });

    const body = (await response.json()) as ApiResponse<T>;

    if (!body.success || body.error) {
      throw new ApiError(
        body.error?.code ?? 'UNKNOWN_ERROR',
        body.error?.message ?? '请求失败',
        response.status,
        body.error?.details ?? []
      );
    }

    return body.data as T;
  }

  get<T>(path: string, init?: RequestInit): Promise<T> {
    return this.request<T>(path, { ...init, method: 'GET' });
  }

  post<T>(path: string, payload: unknown, init?: RequestInit): Promise<T> {
    return this.request<T>(path, {
      ...init,
      method: 'POST',
      body: JSON.stringify(payload)
    });
  }

  /**
   * multipart 上传。全项目唯一的非 JSON 出口（模组导入，`exec/29` 第 5 步）。
   *
   * 🔴 **不设 Content-Type**，交给运行时——见 `request` 里那段注释。
   */
  postForm<T>(path: string, form: FormData, init?: RequestInit): Promise<T> {
    return this.request<T>(path, { ...init, method: 'POST', body: form }, { omitContentType: true });
  }

  put<T>(path: string, payload: unknown, init?: RequestInit): Promise<T> {
    // `init` 是给鉴权头用的（账号级资源要带 Authorization），同 patch/delete。
    return this.request<T>(path, { ...init, method: 'PUT', body: JSON.stringify(payload) });
  }

  patch<T>(path: string, payload: unknown, init?: RequestInit): Promise<T> {
    return this.request<T>(path, {
      ...init,
      method: 'PATCH',
      body: JSON.stringify(payload)
    });
  }

  delete<T>(path: string, init?: RequestInit): Promise<T> {
    return this.request<T>(path, { ...init, method: 'DELETE' });
  }
}
