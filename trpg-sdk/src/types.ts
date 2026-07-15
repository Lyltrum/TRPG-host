/**
 * 这个文件里的类型要跟后端 trpg-backend 的 pydantic 模型手动保持一致
 * （目前项目规模小，靠人工同步；等接口变多之后可以考虑从后端 OpenAPI
 * schema 自动生成，但现在手写更直接）。
 */

/** 对应后端 ErrorDetail：只在 success=false 时出现。 */
export interface ErrorDetail {
  code: string;
  message: string;
}

/** 对应后端 ApiResponse[T]：全项目统一的响应信封形状。 */
export interface ApiResponse<T> {
  success: boolean;
  data: T | null;
  error: ErrorDetail | null;
}

/** 对应后端 dto/example.py 里的 ExampleRead（GET/POST/PUT 的返回形状）。 */
export interface Example {
  id: string;
  name: string;
  description: string | null;
  created_at: string;
  updated_at: string;
}

/** 对应后端 ExampleCreate：POST 请求体。 */
export interface ExampleCreateInput {
  name: string;
  description?: string | null;
}

/** 对应后端 ExampleUpdate：PUT 请求体。 */
export interface ExampleUpdateInput {
  name: string;
  description?: string | null;
}
