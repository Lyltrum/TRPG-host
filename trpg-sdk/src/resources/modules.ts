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

  private authenticated(token: string): RequestInit {
    return { headers: { Authorization: `Bearer ${token}` } };
  }

  /**
   * GET /api/v1/modules/{moduleId} — 模组详情。
   *
   * 🔴 `token` 是**必填**（2026-08-19）：导入的模组有主，后端按
   * 「内置 / 我导入的 / 我正在某个用了它的房间里」判受众。做成可选参数的话，
   * 漏传就静默退化成一次未登录请求——那正是这个仓库禁止的静默兜底。
   */
  getDetail(moduleId: string, token: string): Promise<ModuleDetail> {
    return this.client.get<ModuleDetail>(`/modules/${moduleId}`, this.authenticated(token));
  }

  /**
   * GET /api/v1/modules/import — 我的导入记录（含正在转的那条）。
   *
   * 「我的模组」那一屏靠它回答"关掉页面之后怎么回来"，所以它返回的不只是终态。
   */
  listImportJobs(token: string): Promise<ModuleImportJob[]> {
    return this.client.get<ModuleImportJob[]>('/modules/import', this.authenticated(token));
  }

  /**
   * POST /api/v1/modules/import — 上传模组正文，起一个后台转换任务。
   *
   * 只收**一个文件**（pdf/docx/doc/txt）；压缩包会被后端明确拒绝并说明原因。
   */
  startImport(token: string, file: File | Blob, filename?: string): Promise<ModuleImportJob> {
    const form = new FormData();
    // 第三个参数是文件名。Blob 没有 name，不显式给的话后端会收到
    // "blob" 这个占位名——用户看到的就是一条叫 blob 的导入记录。
    form.append('file', file, filename ?? (file instanceof File ? file.name : 'module'));
    return this.client.postForm<ModuleImportJob>(
      '/modules/import',
      form,
      this.authenticated(token)
    );
  }

  /**
   * GET /api/v1/modules/import/{jobId} — 轮询导入任务状态。
   *
   * 🔴 `token` 是**必填**（2026-08-19 补鉴权）：这个端点此前连登录都不要，
   * 而 job 里带着 `sourceFilename`——用户自己的文件名。同 `getDetail`，不做成
   * 可选参数：漏传会静默退化成一次未登录请求，而那正是禁止的静默兜底。
   */
  getImportJob(jobId: string, token: string): Promise<ModuleImportJob> {
    return this.client.get<ModuleImportJob>(
      `/modules/import/${jobId}`,
      this.authenticated(token)
    );
  }

  /**
   * DELETE /api/v1/modules/{moduleId} — 把自己导入的模组删掉。
   *
   * 🔴 **有房间在用会 409**，错误消息里带着房间数与出路（解散）。别把它当成
   * 「删不掉就算了」——那句话是给用户看的下一步。
   *
   * 内置模组删不掉（无主），别人的模组表现为 404（不确认存在性）。
   */
  deleteModule(token: string, moduleId: string): Promise<null> {
    return this.client.delete<null>(`/modules/${moduleId}`, this.authenticated(token));
  }

  /**
   * DELETE /api/v1/modules/import/{jobId} — 把一条导入记录从列表里抹掉。
   *
   * 删的是**记录**，不是模组。还连着一份模组的记录会 409——那份模组要清得走
   * `deleteModule`，否则「我的模组」失去它唯一的入口就再也删不掉了。
   * 正在转的那条也会 409：后台还在往它上面写。
   */
  deleteImportJob(token: string, jobId: string): Promise<null> {
    return this.client.delete<null>(`/modules/import/${jobId}`, this.authenticated(token));
  }

  /**
   * POST /api/v1/modules/import/{jobId}/retry — 重跑一次。
   *
   * 🔴 **返回的是一个新 job**：旧 job 的失败理由要留着，否则用户点三次就再也
   * 不知道前两次为什么失败。重跑由用户点，不自动（那等于默默再花一次钱）。
   */
  retryImport(token: string, jobId: string): Promise<ModuleImportJob> {
    return this.client.post<ModuleImportJob>(
      `/modules/import/${jobId}/retry`,
      {},
      this.authenticated(token)
    );
  }
}
