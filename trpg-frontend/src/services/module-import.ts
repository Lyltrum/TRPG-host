import type { ModuleImportJob } from 'trpg-sdk'
import { getAuthToken, sdk } from './api-client'

export type { ModuleImportJob }

/** 导入是账号级资产，全部要求登录。 */
function requireAuthToken(): string {
  const token = getAuthToken()
  if (!token) throw new Error('请先登录')
  return token
}

/** 上传模组正文，起一个后台转换任务。只收一个文件。 */
export async function startModuleImport(file: File): Promise<ModuleImportJob> {
  return sdk.modules.startImport(requireAuthToken(), file)
}

/** 我的导入记录（含正在转的）。「我的模组」那一屏靠它。 */
export async function listModuleImports(): Promise<ModuleImportJob[]> {
  return sdk.modules.listImportJobs(requireAuthToken())
}

export async function getModuleImport(jobId: string): Promise<ModuleImportJob> {
  return sdk.modules.getImportJob(jobId)
}

/** 重跑。返回的是**一个新 job**，调用方要拿新的 jobId 继续轮询。 */
export async function retryModuleImport(jobId: string): Promise<ModuleImportJob> {
  return sdk.modules.retryImport(requireAuthToken(), jobId)
}

/** 六个阶段的用户可见说法。取值来自后端 `job_state.STAGES`。 */
export const STAGE_LABELS: Record<string, string> = {
  received: '收下文件',
  extracting: '读取文稿',
  probing: '拆解条目',
  relating: '梳理线索关系',
  assembling: '组装剧本',
  registering: '收进你的模组库',
}

/** 顺序即进度。跟后端 `STAGES` 一致。 */
export const STAGE_ORDER = [
  'received',
  'extracting',
  'probing',
  'relating',
  'assembling',
  'registering',
] as const

/** 硬失败类别的中文说法。后端只给封闭集合里的词（不给错误原文，那里面有剧透）。 */
export const FAILURE_KIND_LABELS: Record<string, string> = {
  schema: '结构不合法',
  ref: '引用对不上',
  skill: '技能名',
  orphan: '有内容没归位',
  leak: '真相泄露',
  facts: '线索表',
  thin_slot: '开场信息过多',
  secret_public: '机密进了公开区',
  structure: '结构不完整',
  trace: '追不回原文',
  numeric: '数值对不上',
}

export function isJobRunning(job: ModuleImportJob): boolean {
  return job.status === 'pending' || job.status === 'running'
}

/**
 * 进度百分比。
 *
 * 🔴 **未知阶段返回 0 而不是猜一个**——阶段名对不上说明前后端不同步，
 * 这时候画一个假的进度比不画更糟。
 */
export function stageProgress(stage: string | undefined): number {
  const i = STAGE_ORDER.indexOf((stage ?? '') as (typeof STAGE_ORDER)[number])
  if (i < 0) return 0
  return Math.round(((i + 1) / STAGE_ORDER.length) * 100)
}
