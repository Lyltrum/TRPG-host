import type { ApiClient } from '../client';
import type { ModuleDetail, ModuleImportJob } from '../types';

/**
 * `/api/v1/modules` 里除「列表」之外的接口——模组详情 + 导入。
 * （`GET /modules` 列表在 RoomsResource.listModules，沿用原有位置不迁移。）
 *
 * 导入是「上传 + 轮询」两段式：转换要跑 5–26 分钟，`startImport` 只是把文件交
 * 上去、拿一个 job 回来，进度靠 `getImportJob` 轮询。
 *
 * 🔴 **job 里只有数量与拓扑，没有模组内容**——导入的人就是即将开玩的玩家，
 * 让他看见正文就等于剧透（`exec/29 §2`）。这条约束在后端的 DTO 与表结构上执行，
 * SDK 这里只是照搬类型。
 */
export class ModulesResource {
  constructor(private readonly client: ApiClient) {}

  /** GET /api/v1/modules/{moduleId} — 模组详情 */
  getDetail(moduleId: string): Promise<ModuleDetail> {
    return this.client.get<ModuleDetail>(`/modules/${moduleId}`);
  }

  /**
   * POST /api/v1/modules/import — 上传模组正文，起一个后台转换任务。
   *
   * 只收**一个文件**（pdf/docx/doc/txt）；压缩包会被后端明确拒绝并说明原因。
   */
  startImport(file: File | Blob, filename?: string): Promise<ModuleImportJob> {
    const form = new FormData();
    // 第三个参数是文件名。Blob 没有 name，不显式给的话后端会收到
    // "blob" 这个占位名——用户看到的就是一条叫 blob 的导入记录。
    form.append('file', file, filename ?? (file instanceof File ? file.name : 'module'));
    return this.client.postForm<ModuleImportJob>('/modules/import', form);
  }

  /** GET /api/v1/modules/import/{jobId} — 轮询导入任务状态 */
  getImportJob(jobId: string): Promise<ModuleImportJob> {
    return this.client.get<ModuleImportJob>(`/modules/import/${jobId}`);
  }

  /**
   * POST /api/v1/modules/import/{jobId}/retry — 重跑一次。
   *
   * 🔴 **返回的是一个新 job**：旧 job 的失败理由要留着，否则用户点三次就再也
   * 不知道前两次为什么失败。重跑由用户点，不自动（那等于默默再花一次钱）。
   */
  retryImport(jobId: string): Promise<ModuleImportJob> {
    return this.client.post<ModuleImportJob>(`/modules/import/${jobId}/retry`, {});
  }
}
