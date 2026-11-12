<script setup>
// 左侧创作面板 v2：四种模式（文生/图生/视频参考/编辑）+ 全模态素材上传 + 官方提示词素材引用芯片。
import { computed, ref, watch } from 'vue'
import { estimateCost, materialRefChips, parseDurationInput, validateLocalVideoFile } from '../api.js'
import {
  ALLOWED_IMAGE_TYPES,
  DURATIONS,
  IMAGE_EXTENSIONS,
  MAX_AUDIO_MB,
  MAX_AUDIOS,
  MAX_IMAGE_BYTES,
  MAX_IMAGE_MB,
  MAX_IMAGES,
  MAX_VIDEOS,
  MODES,
  RATIOS,
  RESOLUTIONS,
  VIDEO_RULES,
} from '../constants.js'

const props = defineProps({
  mode: { type: String, default: null }, // 'mock' | 'real'
  pricePerSecond: { type: Number, default: null },
  promptHistory: { type: Array, default: () => [] },
  submitting: { type: Boolean, default: false },
})

const emit = defineEmits([
  'update:draft',
  'submit',
  'clear-prompt',
  'pick-history',
  'clear-history',
  'notify',
  'upload-video',
  'retry-video',
  'remove-video',
])

const draft = defineModel('draft', { required: true })

const showHistory = ref(false)
const promptInput = ref(null)

const activeMode = computed(
  () => MODES.find((item) => item.value === draft.value.mode) ?? MODES[0],
)

const refChips = computed(() =>
  materialRefChips({
    images: draft.value.images,
    videoUrls: draft.value.videoUrls,
    videoUploads: draft.value.videoUploads,
    audioUrls: draft.value.audioUrls,
  }),
)

const imageCount = computed(
  () => (draft.value.images ?? []).filter((item) => item && item.file).length,
)

const videoCount = computed(
  () =>
    (draft.value.videoUrls ?? []).filter((url) => url && url.trim()).length +
    (draft.value.videoUploads ?? []).length,
)

const estimatedCost = computed(() =>
  estimateCost(draft.value.duration, props.pricePerSecond),
)

const costLine = computed(() => {
  if (props.mode === 'mock') return { text: '模拟模式 · 不会消耗真实额度', kind: 'mock' }
  if (estimatedCost.value !== null) {
    return {
      text: `预计 ¥${estimatedCost.value.toFixed(2)}（${props.pricePerSecond} 元/秒 × ${draft.value.duration}s）`,
      kind: 'priced',
    }
  }
  return { text: '价格未知 · 提交将消耗 TokenHub 额度', kind: 'warn' }
})

// ---------------------------------------------------------------- 素材引用

function insertRef(chip) {
  const textarea = promptInput.value
  const prompt = draft.value.prompt ?? ''
  let next
  if (textarea && typeof textarea.selectionStart === 'number') {
    const start = textarea.selectionStart
    const end = textarea.selectionEnd
    next = `${prompt.slice(0, start)}${chip.label}${prompt.slice(end)}`
    draft.value.prompt = next
    requestAnimationFrame(() => {
      textarea.focus()
      const caret = start + chip.label.length
      textarea.setSelectionRange(caret, caret)
    })
  } else {
    next = prompt ? `${prompt} ${chip.label}` : chip.label
    draft.value.prompt = next
  }
}

// ---------------------------------------------------------------- 模式切换清素材

// 切换模式时清空全部素材，防止旧模式的图片/视频/音频被静默带进新模式的
// 生成请求（不同模式的素材语义不同，且可能导致超出预期的计费）。
watch(
  () => draft.value.mode,
  (after, before) => {
    if (after === before) return
    const hadMaterials =
      draft.value.images.length > 0 ||
      draft.value.videoUrls.length > 0 ||
      draft.value.videoUploads.length > 0 ||
      draft.value.audioUrls.length > 0
    if (!hadMaterials) return
    for (const item of draft.value.images) {
      if (item.preview) URL.revokeObjectURL(item.preview)
    }
    draft.value.images = []
    draft.value.videoUrls = []
    for (const item of draft.value.videoUploads) emit('remove-video', item)
    draft.value.videoUploads = []
    draft.value.audioUrls = []
    emit('notify', { kind: 'warning', text: '已切换模式，素材已清空（不同模式的素材语义不同）' })
  },
)

// ---------------------------------------------------------------- 图片上传

function pickImage(role) {
  const input = document.createElement('input')
  input.type = 'file'
  input.accept = '.jpg,.jpeg,.png,.webp,image/jpeg,image/png,image/webp'
  input.onchange = () => {
    const file = input.files?.[0]
    if (file) addImage(file, role)
  }
  input.click()
}

function validateImageFile(file) {
  const extension = (file.name.split('.').pop() ?? '').toLowerCase()
  if (!ALLOWED_IMAGE_TYPES.includes(file.type) || !IMAGE_EXTENSIONS.includes(extension)) {
    emit('notify', { kind: 'error', text: '图片格式不支持：仅支持 JPG、PNG、WebP' })
    return false
  }
  if (file.size > MAX_IMAGE_BYTES) {
    emit('notify', { kind: 'error', text: `图片大小超过限制（最大 ${MAX_IMAGE_MB}MB）` })
    return false
  }
  return true
}

function addImage(file, role) {
  if (!validateImageFile(file)) return
  if (imageCount.value >= MAX_IMAGES) {
    emit('notify', { kind: 'error', text: `参考图片数量已达上限（最多 ${MAX_IMAGES} 张）` })
    return
  }
  draft.value.images.push({ file, preview: URL.createObjectURL(file), role })
}

function removeImageAt(index) {
  const [removed] = draft.value.images.splice(index, 1)
  if (removed?.preview) URL.revokeObjectURL(removed.preview)
}

// ---------------------------------------------------------------- 视频/音频 URL

function addVideoUrl() {
  if (videoCount.value >= MAX_VIDEOS) {
    emit('notify', { kind: 'error', text: `参考视频数量已达上限（最多 ${MAX_VIDEOS} 个）` })
    return
  }
  draft.value.videoUrls = [...(draft.value.videoUrls ?? []), '']
}

function pickVideoFile() {
  if (videoCount.value >= MAX_VIDEOS) {
    emit('notify', { kind: 'error', text: `参考视频数量已达上限（最多 ${MAX_VIDEOS} 个）` })
    return
  }
  const input = document.createElement('input')
  input.type = 'file'
  input.accept = '.mp4,.mov,video/mp4,video/quicktime'
  input.onchange = () => {
    const file = input.files?.[0]
    if (!file) return
    const error = validateLocalVideoFile(file)
    if (error) {
      emit('notify', { kind: 'error', text: error })
      return
    }
    emit('upload-video', file)
  }
  input.click()
}

function formatBytes(value) {
  if (!Number.isFinite(value)) return ''
  return `${(value / (1024 * 1024)).toFixed(1)}MB`
}

function removeVideoAt(index) {
  draft.value.videoUrls = draft.value.videoUrls.filter((_, i) => i !== index)
}

function addAudioUrl() {
  if ((draft.value.audioUrls ?? []).length >= MAX_AUDIOS) {
    emit('notify', { kind: 'error', text: `参考音频数量已达上限（最多 ${MAX_AUDIOS} 个）` })
    return
  }
  draft.value.audioUrls = [...(draft.value.audioUrls ?? []), '']
}

function removeAudioAt(index) {
  draft.value.audioUrls = draft.value.audioUrls.filter((_, i) => i !== index)
}

function pickAudioFile(index) {
  const input = document.createElement('input')
  input.type = 'file'
  input.accept = '.wav,.mp3,audio/wav,audio/mpeg'
  input.onchange = () => {
    const file = input.files?.[0]
    if (!file) return
    if (file.size > MAX_AUDIO_MB * 1024 * 1024) {
      emit('notify', { kind: 'error', text: `音频大小超过限制（最大 ${MAX_AUDIO_MB}MB）` })
      return
    }
    const reader = new FileReader()
    reader.onload = () => {
      const dataUri = String(reader.result)
      const list = [...(draft.value.audioUrls ?? [])]
      list[index] = dataUri
      draft.value.audioUrls = list
    }
    reader.readAsDataURL(file)
  }
  input.click()
}

// ---------------------------------------------------------------- 时长

function onDurationInput(event) {
  const parsed = parseDurationInput(event.target.value)
  if (parsed === null) {
    emit('notify', { kind: 'error', text: '时长必须是 4~15 秒之间的整数' })
    event.target.value = draft.value.duration
    return
  }
  draft.value.duration = parsed
}

// ---------------------------------------------------------------- 校验与提交

const textPromptInvalid = computed(
  () => draft.value.mode === 'text' && !(draft.value.prompt ?? '').trim(),
)

function submit() {
  if (textPromptInvalid.value) {
    emit('notify', { kind: 'error', text: '请先填写提示词' })
    return
  }
  if (draft.value.mode === 'image' && imageCount.value === 0 && !(draft.value.prompt ?? '').trim()) {
    emit('notify', { kind: 'error', text: '请上传图片或填写提示词' })
    return
  }
  if ((draft.value.videoUploads ?? []).some((item) => item.status === 'uploading')) {
    emit('notify', { kind: 'warning', text: '本地视频仍在上传，请等待素材可用' })
    return
  }
  if ((draft.value.videoUploads ?? []).some((item) => item.status === 'failed')) {
    emit('notify', { kind: 'error', text: '存在上传失败的视频，请重试或移除' })
    return
  }
  if (draft.value.mode === 'edit' && videoCount.value === 0) {
    emit('notify', { kind: 'error', text: '编辑视频模式必须提供至少一个待编辑的参考视频' })
    return
  }
  emit('submit')
}
</script>

<template>
  <section class="panel">
    <h2 class="panel-title">创作面板</h2>
    <p class="panel-sub">doubao-seedance-2.0 · 全模态创作</p>

    <!-- 模式切换 -->
    <div class="segmented" role="tablist" aria-label="生成模式">
      <button
        v-for="item in MODES"
        :key="item.value"
        class="segment"
        :class="{ 'is-active': draft.mode === item.value }"
        role="tab"
        @click="draft.mode = item.value"
      >
        {{ item.label }}
      </button>
    </div>
    <p class="mode-hint">{{ activeMode.hint }}</p>

    <!-- 提示词 + 素材引用芯片 -->
    <div class="field">
      <div class="field-head">
        <label class="field-label" for="prompt-input">
          {{ draft.mode === 'text' ? '画面描述' : '运动 / 编辑描述' }}
        </label>
        <div class="field-actions">
          <button class="mini-btn" :disabled="!promptHistory.length" @click="showHistory = !showHistory">
            历史
          </button>
          <button class="mini-btn" :disabled="!draft.prompt" @click="emit('clear-prompt')">清空</button>
        </div>
      </div>

      <div v-if="refChips.length" class="material-refs">
        <button
          v-for="chip in refChips"
          :key="`${chip.kind}-${chip.label}`"
          class="ref-chip"
          :title="`点击在光标处插入「${chip.label}」占位（官方提示词规则：素材类型+序号）`"
          @click="insertRef(chip)"
        >
          {{ chip.label }}
        </button>
      </div>

      <textarea
        id="prompt-input"
        ref="promptInput"
        v-model="draft.prompt"
        class="textarea"
        maxlength="8000"
        :placeholder="
          draft.mode === 'text'
            ? '描述你想生成的画面，例如：电影感画面，一辆未来跑车驶过雨夜街道，霓虹灯在车身反射……'
            : draft.mode === 'edit'
              ? '例如：将视频1中的主角替换成图片1中的人物，运镜不变'
              : '例如：全程使用视频1的运镜，人物形象参考图片1，背景音乐使用音频1……'
        "
      ></textarea>
      <div class="char-count">{{ draft.prompt.length }} / 8000</div>

      <Transition name="fade">
        <div v-if="showHistory && promptHistory.length" class="history-pop">
          <div class="history-head">
            <span>最近使用的提示词</span>
            <button class="mini-btn" @click="emit('clear-history')">清除全部</button>
          </div>
          <button
            v-for="item in promptHistory"
            :key="item"
            class="history-item"
            @click="emit('pick-history', item)"
          >
            {{ item }}
          </button>
        </div>
      </Transition>
    </div>

    <!-- 图生视频：首帧 / 尾帧 / 参考图 -->
    <div v-if="draft.mode === 'image'" class="field">
      <span class="field-label">帧与参考图（JPG / PNG / WebP，≤{{ MAX_IMAGE_MB }}MB，共 ≤{{ MAX_IMAGES }} 张）</span>
      <div class="material-grid" style="margin-top: 10px">
        <div
          v-for="(item, index) in draft.images"
          :key="`${item.role}-${index}`"
          class="material-card"
        >
          <img :src="item.preview" alt="素材预览" />
          <span class="material-tag">
            {{ item.role === 'first_frame' ? '首帧' : item.role === 'last_frame' ? '尾帧' : `参考${index + 1}` }}
          </span>
          <button class="material-remove" title="移除" @click="removeImageAt(index)">×</button>
        </div>
        <div v-if="imageCount < MAX_IMAGES" class="material-card">
          <button class="mat-icon" @click="pickImage('first_frame')">
            <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round">
              <path d="M12 5v14M5 12h14" />
            </svg>
          </button>
          <span class="material-tag">首帧</span>
        </div>
        <div v-if="imageCount < MAX_IMAGES" class="material-card">
          <button class="mat-icon" @click="pickImage('last_frame')">
            <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round">
              <path d="M12 5v14M5 12h14" />
            </svg>
          </button>
          <span class="material-tag">尾帧</span>
        </div>
        <div v-if="imageCount < MAX_IMAGES" class="material-card">
          <button class="mat-icon" @click="pickImage('reference_image')">
            <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round">
              <path d="M12 5v14M5 12h14" />
            </svg>
          </button>
          <span class="material-tag">参考</span>
        </div>
      </div>
    </div>

    <!-- 视频参考 / 编辑视频：视频 URL + 参考图 -->
    <div v-if="draft.mode === 'video' || draft.mode === 'edit'" class="field">
      <div class="field-head">
        <span class="field-label">{{ draft.mode === 'edit' ? '待编辑视频' : '参考视频' }}（≤{{ MAX_VIDEOS }} 个）</span>
        <div class="field-actions">
          <button class="mini-btn" :disabled="videoCount >= MAX_VIDEOS" @click="pickVideoFile">上传本地视频</button>
          <button class="mini-btn" :disabled="videoCount >= MAX_VIDEOS" @click="addVideoUrl">添加链接</button>
        </div>
      </div>
      <div v-for="item in draft.videoUploads" :key="item.localId" class="local-video-card" :class="`is-${item.status}`">
        <div class="local-video-main">
          <strong>{{ item.fileName }}</strong>
          <span v-if="item.status === 'uploading'">正在上传并建立安全通道…</span>
          <span v-else-if="item.status === 'ready'">
            素材可用 · {{ formatBytes(item.size) }} · {{ Number(item.duration).toFixed(1) }}秒
          </span>
          <span v-else class="local-video-error">{{ item.error }}</span>
        </div>
        <div class="field-actions">
          <button v-if="item.status === 'failed'" class="mini-btn" @click="emit('retry-video', item)">重试</button>
          <button class="mini-btn" @click="emit('remove-video', item)">移除</button>
        </div>
      </div>
      <div v-for="(url, index) in draft.videoUrls" :key="index" class="url-row">
        <input
          v-model="draft.videoUrls[index]"
          class="url-input"
          type="url"
          placeholder="https://…/白模.mp4 或 asset://素材ID"
        />
        <button class="mini-btn" @click="removeVideoAt(index)">移除</button>
      </div>
      <div v-if="videoCount === 0" class="dropzone" role="button" tabindex="0" @click="pickVideoFile" @keydown.enter="pickVideoFile">
        <span class="dropzone-text">点击从本地上传参考视频</span>
        <span class="dropzone-hint">{{ VIDEO_RULES }}</span>
      </div>
      <p class="field-hint">{{ VIDEO_RULES }}</p>

      <div style="margin-top: 12px">
        <div class="field-head">
          <span class="field-label">参考图（可选，≤{{ MAX_IMAGES }} 张）</span>
          <button class="mini-btn" :disabled="imageCount >= MAX_IMAGES" @click="pickImage('reference_image')">+ 上传</button>
        </div>
        <div v-if="draft.images.length" class="material-grid">
          <div v-for="(item, index) in draft.images" :key="index" class="material-card">
            <img :src="item.preview" alt="素材预览" />
            <span class="material-tag">参考{{ index + 1 }}</span>
            <button class="material-remove" title="移除" @click="removeImageAt(index)">×</button>
          </div>
        </div>
      </div>
    </div>

    <!-- 参考音频（全模式可选） -->
    <div class="field">
      <div class="field-head">
        <span class="field-label">参考音频（可选，≤{{ MAX_AUDIOS }} 个）</span>
        <button class="mini-btn" @click="addAudioUrl">+ 添加</button>
      </div>
      <div v-for="(value, index) in draft.audioUrls" :key="index" class="url-row">
        <input
          v-model="draft.audioUrls[index]"
          class="url-input"
          type="text"
          :placeholder="value.startsWith('data:') ? '已载入本地音频（Base64）' : 'https://…/bgm.mp3 或选择本地文件'"
        />
        <button class="mini-btn" @click="pickAudioFile(index)">本地文件</button>
        <button class="mini-btn" @click="removeAudioAt(index)">移除</button>
      </div>
      <p class="field-hint">wav / mp3 · ≤{{ MAX_AUDIO_MB }}MB · 音色 / 旋律 / 对话参考</p>
    </div>

    <!-- 参数 -->
    <div class="field">
      <span class="field-label">视频时长（4~15 秒）</span>
      <div class="chip-row" style="margin-top: 8px">
        <button
          v-for="value in DURATIONS"
          :key="value"
          class="chip"
          :class="{ 'is-active': draft.duration === value }"
          @click="draft.duration = value"
        >
          {{ value }}s
        </button>
        <label class="duration-custom">
          <input
            class="duration-input"
            type="number"
            min="4"
            max="15"
            step="1"
            :value="draft.duration"
            @change="onDurationInput"
          />
          <span class="duration-unit">秒</span>
        </label>
      </div>
    </div>

    <div class="field">
      <span class="field-label">分辨率</span>
      <div class="chip-row" style="margin-top: 8px">
        <button v-for="item in RESOLUTIONS" :key="item.value" class="chip" :class="{ 'is-active': draft.resolution === item.value }" @click="draft.resolution = item.value">
          {{ item.label }}
        </button>
      </div>
    </div>

    <div class="field">
      <span class="field-label">画面比例</span>
      <div class="chip-row" style="margin-top: 8px">
        <button v-for="value in RATIOS" :key="value" class="chip ratio-chip" :class="{ 'is-active': draft.ratio === value }" @click="draft.ratio = value">
          <span class="ratio-rect" :class="`ratio-${value.replace(':', '-')}`"></span>
          {{ value }}
        </button>
      </div>
    </div>

    <!-- 开关组 -->
    <div class="field" style="display: flex; flex-direction: column; gap: 9px">
      <div class="inline-field">
        <div>
          <span class="field-label">音频生成</span>
          <span class="field-hint">为视频生成背景音</span>
        </div>
        <button
          class="switch"
          :class="{ 'is-on': draft.generateAudio }"
          role="switch"
          :aria-checked="draft.generateAudio"
          @click="draft.generateAudio = !draft.generateAudio"
        >
          <span class="switch-knob"></span>
        </button>
      </div>

      <div class="inline-field">
        <div>
          <span class="field-label">返回尾帧图</span>
          <span class="field-hint">成功后可在舞台查看 / 下载尾帧</span>
        </div>
        <button
          class="switch"
          :class="{ 'is-on': draft.returnLastFrame }"
          role="switch"
          :aria-checked="draft.returnLastFrame"
          @click="draft.returnLastFrame = !draft.returnLastFrame"
        >
          <span class="switch-knob"></span>
        </button>
      </div>

      <div v-if="draft.mode === 'text'" class="inline-field">
        <div>
          <span class="field-label">联网搜索</span>
          <span class="field-hint">仅纯文本模式生效，可提升时效性</span>
        </div>
        <button
          class="switch"
          :class="{ 'is-on': draft.webSearch }"
          role="switch"
          :aria-checked="draft.webSearch"
          @click="draft.webSearch = !draft.webSearch"
        >
          <span class="switch-knob"></span>
        </button>
      </div>
    </div>

    <!-- Mock 模式专属 -->
    <div v-if="mode === 'mock'" class="field">
      <span class="field-label">模拟结果（仅 Mock 模式）</span>
      <div class="chip-row" style="margin-top: 8px">
        <button class="chip" :class="{ 'is-active': draft.mockOutcome === 'success' }" @click="draft.mockOutcome = 'success'">成功</button>
        <button class="chip" :class="{ 'is-active': draft.mockOutcome === 'fail' }" @click="draft.mockOutcome = 'fail'">失败</button>
      </div>
    </div>

    <!-- 费用与提交 -->
    <div class="cost-card" :class="`is-${costLine.kind}`">
      <span>{{ costLine.text }}</span>
    </div>
    <button class="btn-primary" :disabled="submitting" @click="submit">
      <span v-if="submitting" class="spinner"></span>
      {{ submitting ? '提交中…' : '生成视频' }}
    </button>
  </section>
</template>
