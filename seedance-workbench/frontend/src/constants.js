// 参数常量（与后端 validation.py 保持一致；后端仍是最终裁决者）。

export const DURATIONS = [4, 5, 8, 10, 12, 15]

export const RESOLUTIONS = [
  { value: '480p', label: '480p' },
  { value: '720p', label: '720p' },
  { value: '1080p', label: '1080p' },
  { value: '4k', label: '4K' },
]

export const RATIOS = ['16:9', '9:16', '1:1', '4:3', '3:4', '21:9']

export const MAX_IMAGE_BYTES = 8 * 1024 * 1024
export const MAX_IMAGE_MB = 8

export const ALLOWED_IMAGE_TYPES = ['image/jpeg', 'image/png', 'image/webp']
export const IMAGE_EXTENSIONS = ['jpg', 'jpeg', 'png', 'webp']

// v2：生成模式（与后端 MODES 一致）
export const MODES = [
  { value: 'text', label: '文生视频', hint: '纯文字描述画面，模型从零生成' },
  {
    value: 'image',
    label: '图生视频',
    hint: '上传首帧（可选尾帧），从图片出发生成视频',
  },
  {
    value: 'video',
    label: '视频参考',
    hint: '上传白模/参考视频学运镜与动作，可配人物图组合参考',
  },
  {
    value: 'edit',
    label: '编辑视频',
    hint: '替换视频主体、对象增删改、局部重绘，运镜保持不变',
  },
]

// v2：素材数量上限（与后端一致）
export const MAX_IMAGES = 9
export const MAX_VIDEOS = 3
export const MAX_AUDIOS = 3
export const MAX_AUDIO_MB = 15

// 官方视频参考规格提示
export const VIDEO_RULES = '本地 MP4/MOV 或公网 HTTPS/asset:// · 单个 2~15 秒 · ≤50MB'
