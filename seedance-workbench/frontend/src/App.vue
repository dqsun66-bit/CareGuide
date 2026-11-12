<script setup>
// Seedance 工作台主应用：状态编排、轮询、防重复提交、历史记录与通知。
import { onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import {
  ApiError,
  addPromptHistory,
  buildClientRequestId,
  createGeneration,
  deleteTask,
  getTask,
  getTaskByCrid,
  health,
  isNotFoundError,
  isUuid,
  listTasks,
  loadPromptHistory,
  refreshTask,
  removeVideoUpload,
  savePromptHistory,
  shouldClearPendingKey,
  videoUrl,
  uploadVideo,
} from './api.js'
import CreatorPanel from './components/CreatorPanel.vue'
import TaskHistory from './components/TaskHistory.vue'
import VideoStage from './components/VideoStage.vue'

const PENDING_CRID_KEY = 'seedance.pendingClientRequestId'
const POLL_INTERVAL_MS = 3000
const ACTIVE_STATUSES = new Set(['submitted', 'queued', 'running'])
const PAGE_SIZE = 10

const appMode = ref(null) // 'mock' | 'real'
const pricePerSecond = ref(null)
const submitting = ref(false)
const currentTask = ref(null)
const tasks = ref([])
const total = ref(0)
const page = ref(1)
const promptHistory = ref(loadPromptHistory())
const toasts = ref([])
const clientRequestId = ref(null)

const draft = reactive({
  mode: 'text',
  prompt: '',
  duration: 5,
  resolution: '480p',
  ratio: '16:9',
  generateAudio: false,
  images: [], // [{file, preview, role}]
  videoUrls: [],
  videoUploads: [],
  audioUrls: [],
  returnLastFrame: false,
  webSearch: false,
  mockOutcome: 'success',
})

let pollTimer = null
let consecutivePollFailures = 0

const MAX_POLL_FAILURES = 5

// ---------------------------------------------------------------- 通知

function notify(kind, text) {
  const id = `${Date.now()}-${Math.random()}`
  toasts.value.push({ id, kind, text })
  setTimeout(() => {
    toasts.value = toasts.value.filter((toast) => toast.id !== id)
  }, 5000)
}

// ---------------------------------------------------------------- 幂等键

function savePendingCrid(crid) {
  try {
    sessionStorage.setItem(PENDING_CRID_KEY, crid)
  } catch {
    // 隐私模式等场景下 sessionStorage 不可用：不阻断提交，但丢失刷新恢复能力
  }
}

function clearPendingCrid() {
  try {
    sessionStorage.removeItem(PENDING_CRID_KEY)
  } catch {
    // 同上：忽略
  }
  clientRequestId.value = null
}

function readPendingCrid() {
  try {
    return sessionStorage.getItem(PENDING_CRID_KEY)
  } catch {
    return null
  }
}

// ---------------------------------------------------------------- 轮询

function stopPolling() {
  if (pollTimer !== null) {
    clearTimeout(pollTimer)
    pollTimer = null
  }
}

function isTerminal(status) {
  return !ACTIVE_STATUSES.has(status)
}

function startPolling() {
  stopPolling()
  pollTimer = setTimeout(async () => {
    if (!currentTask.value) return
    try {
      const body = await getTask(currentTask.value.task_id)
      consecutivePollFailures = 0
      currentTask.value = body.data
      // 仅当到达确定终态且属于当前幂等键对应的任务时才清除（防重复扣费）
      if (shouldClearPendingKey(body.data, readPendingCrid())) {
        clearPendingCrid()
      }
      if (isTerminal(body.data.status)) {
        stopPolling()
        await loadTasks()
      } else {
        startPolling()
      }
    } catch (error) {
      if (isNotFoundError(error)) {
        stopPolling()
        notify('warning', '任务已被删除，停止轮询')
        return
      }
      consecutivePollFailures += 1
      if (consecutivePollFailures >= MAX_POLL_FAILURES) {
        stopPolling()
        notify('error', '轮询连续失败已停止，可稍后点击「刷新状态」恢复查询')
        return
      }
      startPolling()
    }
  }, POLL_INTERVAL_MS)
}

// ---------------------------------------------------------------- 历史

async function loadTasks() {
  try {
    const body = await listTasks(page.value, PAGE_SIZE)
    tasks.value = body.data.items
    total.value = body.data.total
  } catch (error) {
    notify('error', error.message)
  }
}

// ---------------------------------------------------------------- 提交

async function submit() {
  submitting.value = true
  const crid = isUuid(clientRequestId.value) ? clientRequestId.value : buildClientRequestId()
  clientRequestId.value = crid
  savePendingCrid(crid)

  try {
    const body = await createGeneration({
      mode: draft.mode,
      prompt: draft.prompt,
      duration: draft.duration,
      resolution: draft.resolution,
      ratio: draft.ratio,
      generateAudio: draft.generateAudio,
      clientRequestId: crid,
      images: draft.images,
      videoUrls: draft.videoUrls,
      videoUploads: draft.videoUploads,
      audioUrls: draft.audioUrls,
      returnLastFrame: draft.returnLastFrame,
      webSearch: draft.webSearch,
      mockOutcome: appMode.value === 'mock' ? draft.mockOutcome : null,
    })
    const task = body.data
    currentTask.value = task
    if (task.status === 'failed') {
      notify('error', task.error_message || '生成失败')
    } else if (task.status === 'unknown') {
      notify('warning', task.error_message || '提交结果未知，请勿重复提交')
    }
    if (shouldClearPendingKey(task, crid)) {
      // 确定终态：可以安全清除幂等键
      clearPendingCrid()
    } else if (ACTIVE_STATUSES.has(task.status)) {
      startPolling()
    }
    // unknown：保留幂等键与 sessionStorage 记录，重试/刷新复用同一键，绝不重复 POST
    if (draft.prompt.trim()) {
      promptHistory.value = addPromptHistory(draft.prompt, promptHistory.value)
      savePromptHistory(promptHistory.value)
    }
    await loadTasks()
  } catch (error) {
    // 保留 clientRequestId：重试时复用同一幂等键，绝不重复扣费
    const message = error instanceof ApiError ? error.message : '提交失败，请稍后再试'
    notify('error', message)
  } finally {
    submitting.value = false
  }
}

// ---------------------------------------------------------------- 本地视频上传

const videoUploadControllers = new Map()

async function uploadLocalVideo(file, existing = null) {
  const item = existing ?? {
    localId: buildClientRequestId(),
    file,
    fileName: file.name,
    size: file.size,
    status: 'uploading',
    uploadId: null,
    duration: null,
    error: null,
  }
  if (!existing) draft.videoUploads.push(item)
  item.status = 'uploading'
  item.error = null
  const controller = new AbortController()
  videoUploadControllers.set(item.localId, controller)
  try {
    const body = await uploadVideo(file, { signal: controller.signal })
    Object.assign(item, {
      uploadId: body.data.upload_id,
      fileName: body.data.filename,
      size: body.data.size,
      duration: body.data.duration,
      status: 'ready',
      error: null,
    })
  } catch (error) {
    if (error.name !== 'AbortError') {
      item.status = 'failed'
      item.error = error instanceof ApiError ? error.message : '视频上传失败，请重试'
    }
  } finally {
    videoUploadControllers.delete(item.localId)
  }
}

async function removeLocalVideo(item) {
  videoUploadControllers.get(item.localId)?.abort()
  videoUploadControllers.delete(item.localId)
  if (item.uploadId) {
    try {
      await removeVideoUpload(item.uploadId)
    } catch (error) {
      if (!isNotFoundError(error)) notify('warning', error.message)
    }
  }
  const index = draft.videoUploads.findIndex((candidate) => candidate.localId === item.localId)
  if (index >= 0) draft.videoUploads.splice(index, 1)
}

function retryLocalVideo(item) {
  if (item.file) uploadLocalVideo(item.file, item)
}

// ---------------------------------------------------------------- 操作

async function refresh(task) {
  try {
    const body = await refreshTask(task.task_id)
    if (currentTask.value && currentTask.value.task_id === task.task_id) {
      currentTask.value = body.data
    }
    await loadTasks()
    if (body.data.status === 'failed') {
      notify('error', body.data.error_message || '任务失败')
    }
  } catch (error) {
    notify('error', error.message)
  }
}

async function remove(task) {
  if (!window.confirm(`确定删除任务 ${task.task_id.slice(0, 18)}… 的记录吗？`)) return
  try {
    await deleteTask(task.task_id)
    if (currentTask.value && currentTask.value.task_id === task.task_id) {
      currentTask.value = null
      stopPolling()
    }
    await loadTasks()
    notify('success', '已删除')
  } catch (error) {
    notify('error', error.message)
  }
}

function download(task) {
  const anchor = document.createElement('a')
  anchor.href = videoUrl(task.task_id)
  anchor.download = `seedance-${task.task_id.slice(0, 12)}.mp4`
  document.body.appendChild(anchor)
  anchor.click()
  anchor.remove()
}

function view(task) {
  currentTask.value = task
  if (ACTIVE_STATUSES.has(task.status)) {
    startPolling()
  }
}

function pickHistory(item) {
  draft.prompt = item
  notify('success', '已填入提示词')
}

// 灵感画廊「生成同款」：把历史任务的提示词与参数复制回创作面板。
// 先清空当前素材（防止旧素材被静默带进新请求），再载入参数。
function recreate(task) {
  for (const item of draft.images) {
    if (item.preview) URL.revokeObjectURL(item.preview)
  }
  draft.images = []
  draft.videoUrls = []
  for (const item of [...draft.videoUploads]) void removeLocalVideo(item)
  draft.videoUploads = []
  draft.audioUrls = []
  draft.mode = ['text', 'image', 'video', 'edit'].includes(task.mode) ? task.mode : 'text'
  draft.prompt = task.prompt ?? ''
  draft.duration = task.duration
  draft.resolution = task.resolution
  draft.ratio = task.ratio
  draft.generateAudio = Boolean(task.generate_audio)
  draft.returnLastFrame = Boolean(task.return_last_frame)
  draft.webSearch = Boolean(task.web_search)
  notify('success', '已载入同款参数，素材请重新上传')
  window.scrollTo({ top: 0, behavior: 'smooth' })
}

function clearPrompt() {
  draft.prompt = ''
}

function clearHistory() {
  promptHistory.value = []
  savePromptHistory([])
}

function changePage(nextPage) {
  page.value = nextPage
  loadTasks()
}

// ---------------------------------------------------------------- 启动

onMounted(async () => {
  try {
    const body = await health()
    appMode.value = body.data.mode
    pricePerSecond.value = body.data.price_cny_per_second
  } catch (error) {
    notify('error', `无法连接后端：${error.message}`)
    return
  }

  await loadTasks()

  // 页面刷新恢复：按幂等键精确找回任务，绝不重新提交
  const pendingCrid = readPendingCrid()
  if (isUuid(pendingCrid)) {
    try {
      const body = await getTaskByCrid(pendingCrid)
      const match = body.data
      currentTask.value = match
      if (!isTerminal(match.status)) {
        clientRequestId.value = pendingCrid
        startPolling()
      } else if (shouldClearPendingKey(match, pendingCrid)) {
        clearPendingCrid()
      }
      // unknown：保留幂等键，重试复用同一键（防重复扣费）
    } catch (error) {
      if (isNotFoundError(error)) {
        // 确认后端不存在该任务：可以清除
        clearPendingCrid()
      } else {
        // 查询失败（后端瞬断等）：保留幂等键，等待下次成功恢复，绝不误删
        clientRequestId.value = pendingCrid
      }
    }
  }
})

onBeforeUnmount(() => {
  stopPolling()
  for (const item of draft.images) {
    if (item.preview) URL.revokeObjectURL(item.preview)
  }
})
</script>

<template>
  <div class="app-shell">
    <header class="app-header">
      <div class="brand">
        <span class="brand-mark">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#fff" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round">
            <path d="M8 5l8 7-8 7" />
          </svg>
        </span>
        <span class="brand-name">Seedance 工作台</span>
        <span class="brand-sub">AI VIDEO STUDIO · 智云 TokenHub</span>
      </div>
      <div class="header-right">
        <span class="badge is-model">doubao-seedance-2.0</span>
        <span v-if="appMode === 'mock'" class="badge is-mock">
          <span class="dot"></span>模拟模式 · 不扣费
        </span>
        <span v-else-if="appMode === 'real'" class="badge is-live">
          <span class="dot"></span>真实模式 · 消耗额度
        </span>
        <span v-else class="badge is-offline">
          <span class="dot"></span>连接后端中…
        </span>
      </div>
    </header>

    <main class="workspace">
      <CreatorPanel
        v-model:draft="draft"
        :mode="appMode"
        :price-per-second="pricePerSecond"
        :prompt-history="promptHistory"
        :submitting="submitting"
        @submit="submit"
        @clear-prompt="clearPrompt"
        @pick-history="pickHistory"
        @clear-history="clearHistory"
        @notify="(payload) => notify(payload.kind, payload.text)"
        @upload-video="uploadLocalVideo"
        @retry-video="retryLocalVideo"
        @remove-video="removeLocalVideo"
      />

      <VideoStage
        :task="currentTask"
        :mode="appMode"
        @refresh="refresh(currentTask)"
        @download="download(currentTask)"
        @notify="(payload) => notify(payload.kind, payload.text)"
      />
    </main>

    <TaskHistory
      class="history-section"
      :tasks="tasks"
      :total="total"
      :page="page"
      :page-size="PAGE_SIZE"
      @view="view"
      @download="download"
      @refresh="refresh"
      @remove="remove"
      @recreate="recreate"
      @page="changePage"
    />

    <!-- 通知 -->
    <div class="toasts" aria-live="polite">
      <TransitionGroup name="fade">
        <div v-for="toast in toasts" :key="toast.id" class="toast" :class="`is-${toast.kind}`">
          {{ toast.text }}
        </div>
      </TransitionGroup>
    </div>
  </div>
</template>

<style scoped>
.history-section {
  margin-top: 22px;
}
</style>
