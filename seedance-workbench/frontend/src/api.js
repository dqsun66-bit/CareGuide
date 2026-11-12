// 后端 API 封装与纯工具函数（可被 node --test 直接测试）。

const API_PREFIX = '/api/seedance'

export const MAX_PROMPT_HISTORY = 20
export const MAX_LOCAL_VIDEO_BYTES = 50 * 1024 * 1024
const PROMPT_HISTORY_KEY = 'seedance.promptHistory'

export class ApiError extends Error {
  constructor(message, { code = 'error', status = 0 } = {}) {
    super(message)
    this.name = 'ApiError'
    this.code = code
    this.status = status
  }
}

// ---------------------------------------------------------------- 纯函数

export function isUuid(value) {
  return (
    typeof value === 'string' &&
    /^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$|^[0-9a-fA-F]{32}$/.test(
      value,
    )
  )
}

export function buildClientRequestId() {
  return globalThis.crypto.randomUUID()
}

export function formDataFromParams(params) {
  const form = new FormData()
  form.append('mode', params.mode)
  form.append('prompt', params.prompt ?? '')
  form.append('duration', String(params.duration))
  form.append('resolution', params.resolution)
  form.append('ratio', params.ratio)
  form.append('generate_audio', String(params.generateAudio))
  form.append('client_request_id', params.clientRequestId)
  if (params.mockOutcome) form.append('mock_outcome', params.mockOutcome)
  // v2：多图按顺序携带角色；重复字段自动成为数组
  const images = (params.images ?? []).filter((item) => item && item.file)
  if (images.length) {
    form.append('image_roles', images.map((item) => item.role).join(','))
    for (const item of images) form.append('images', item.file, item.file.name)
  }
  const videoUrls = (params.videoUrls ?? []).filter((url) => url && url.trim())
  for (const url of videoUrls) form.append('video_urls', url.trim())
  const videoUploads = (params.videoUploads ?? []).filter(
    (item) => item && item.status === 'ready' && item.uploadId,
  )
  for (const item of videoUploads) form.append('video_upload_ids', item.uploadId)
  const audioUrls = (params.audioUrls ?? []).filter((url) => url && url.trim())
  for (const url of audioUrls) form.append('audio_urls', url.trim())
  if (params.returnLastFrame) form.append('return_last_frame', 'true')
  if (params.webSearch) form.append('web_search', 'true')
  return form
}

/** 素材引用标签：按 content 顺序给素材编号（官方提示词规则：素材类型+序号）。 */
export function materialRefLabels({ images = [], videoUrls = [], videoUploads = [], audioUrls = [] } = {}) {
  const imageCount = (images ?? []).filter((item) => item && item.file).length
  const videoCount =
    (videoUrls ?? []).filter((url) => url && url.trim()).length +
    (videoUploads ?? []).filter(Boolean).length
  const audioCount = (audioUrls ?? []).filter((url) => url && url.trim()).length
  return [
    { kind: 'image', label: '图片', count: imageCount },
    { kind: 'video', label: '视频', count: videoCount },
    { kind: 'audio', label: '音频', count: audioCount },
  ]
}

/**
 * 素材引用芯片：为每个已上传素材生成一枚芯片（图片1、图片2、视频1……），
 * 点击插入的编号一定真实存在于 content 中（官方编号从 1 开始按类型计数）。
 */
export function materialRefChips({ images = [], videoUrls = [], videoUploads = [], audioUrls = [] } = {}) {
  const chips = []
  const imageCount = (images ?? []).filter((item) => item && item.file).length
  const videoCount =
    (videoUrls ?? []).filter((url) => url && url.trim()).length +
    (videoUploads ?? []).filter(Boolean).length
  const audioCount = (audioUrls ?? []).filter((url) => url && url.trim()).length
  for (let i = 1; i <= imageCount; i += 1) chips.push({ kind: 'image', label: `图片${i}` })
  for (let i = 1; i <= videoCount; i += 1) chips.push({ kind: 'video', label: `视频${i}` })
  for (let i = 1; i <= audioCount; i += 1) chips.push({ kind: 'audio', label: `音频${i}` })
  return chips
}

export function statusLabel(status) {
  const labels = {
    submitted: '已提交',
    queued: '排队中',
    running: '生成中',
    succeeded: '生成成功',
    failed: '生成失败',
    unknown: '结果未知',
  }
  return labels[status] ?? '未知状态'
}

export function estimateCost(duration, priceCnyPerSecond) {
  if (typeof priceCnyPerSecond !== 'number' || Number.isNaN(priceCnyPerSecond)) return null
  return duration * priceCnyPerSecond
}

/** 视频时长解析：仅接受 4~15 的整数（与后端校验一致），否则返回 null。 */
export function parseDurationInput(value) {
  const number = Number(value)
  if (!Number.isInteger(number) || number < 4 || number > 15) return null
  return number
}

export function validateLocalVideoFile(file) {
  if (!(file instanceof File)) return '请选择视频文件'
  const suffix = file.name.toLowerCase().split('.').pop()
  if (!['mp4', 'mov'].includes(suffix)) return '仅支持 MP4 或 MOV 视频'
  if (file.size > MAX_LOCAL_VIDEO_BYTES) return '视频大小超过限制（最大 50MB）'
  return null
}

export function videoUrl(taskId) {
  return `${API_PREFIX}/tasks/${taskId}/video`
}

export function lastFrameUrl(taskId) {
  return `${API_PREFIX}/tasks/${taskId}/last-frame`
}

/**
 * 防重复扣费核心决策：是否允许清除 sessionStorage 中的幂等键。
 * 仅当任务进入确定终态（成功/失败）且该终态属于当前幂等键对应的任务时才清除；
 * "结果未知"（unknown）绝不能清除——重试必须复用同一键，否则可能重复 POST 扣费。
 */
export function shouldClearPendingKey(task, pendingCrid) {
  if (!task || !pendingCrid) return false
  if (!['succeeded', 'failed'].includes(task.status)) return false
  return task.client_request_id === pendingCrid
}

export function isNotFoundError(error) {
  return error instanceof ApiError && error.status === 404
}

// ------------------------------------------------------- 提示词历史（本地）

export function loadPromptHistory() {
  try {
    const raw = globalThis.localStorage.getItem(PROMPT_HISTORY_KEY)
    if (!raw) return []
    const parsed = JSON.parse(raw)
    return Array.isArray(parsed) ? parsed.filter((item) => typeof item === 'string') : []
  } catch {
    return []
  }
}

export function addPromptHistory(prompt, history) {
  const normalized = String(prompt).trim()
  if (!normalized) return [...history]
  const rest = (history ?? []).filter((item) => item !== normalized)
  return [normalized, ...rest].slice(0, MAX_PROMPT_HISTORY)
}

export function savePromptHistory(history) {
  try {
    globalThis.localStorage.setItem(PROMPT_HISTORY_KEY, JSON.stringify(history))
  } catch {
    // 隐私模式等场景下 localStorage 不可用：忽略，不影响主流程
  }
}

// ---------------------------------------------------------------- 请求层

async function request(path, options = {}) {
  let response
  try {
    response = await fetch(`${API_PREFIX}${path}`, options)
  } catch (error) {
    throw new ApiError(`网络错误：无法连接后端服务（${error.message}）`, { code: 'network' })
  }
  let body
  try {
    body = await response.json()
  } catch {
    throw new ApiError(`后端返回异常（HTTP ${response.status}）`, { code: 'bad_response', status: response.status })
  }
  if (!response.ok || body.ok === false) {
    const error = body.error ?? {}
    throw new ApiError(error.message ?? `请求失败（HTTP ${response.status}）`, {
      code: error.code ?? 'error',
      status: response.status,
    })
  }
  return body
}

export function health() {
  return request('/health')
}

export function createGeneration(params) {
  return request('/generations', { method: 'POST', body: formDataFromParams(params) })
}

export function uploadVideo(file, { signal } = {}) {
  const form = new FormData()
  form.append('video', file, file.name)
  return request('/video-uploads', { method: 'POST', body: form, signal })
}

export function removeVideoUpload(uploadId) {
  return request(`/video-uploads/${encodeURIComponent(uploadId)}`, { method: 'DELETE' })
}

export function getTask(taskId) {
  return request(`/tasks/${encodeURIComponent(taskId)}`)
}

export function getTaskByCrid(clientRequestId) {
  return request(`/tasks/by-crid/${encodeURIComponent(clientRequestId)}`)
}

export function listTasks(page = 1, pageSize = 20) {
  return request(`/tasks?page=${page}&page_size=${pageSize}`)
}

export function refreshTask(taskId) {
  return request(`/tasks/${encodeURIComponent(taskId)}/refresh`)
}

export function deleteTask(taskId) {
  return request(`/tasks/${encodeURIComponent(taskId)}`, { method: 'DELETE' })
}
