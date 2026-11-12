import assert from 'node:assert/strict'
import { test } from 'node:test'

import {
  MAX_PROMPT_HISTORY,
  addPromptHistory,
  buildClientRequestId,
  estimateCost,
  formDataFromParams,
  isNotFoundError,
  isUuid,
  lastFrameUrl,
  loadPromptHistory,
  materialRefLabels,
  materialRefChips,
  parseDurationInput,
  shouldClearPendingKey,
  statusLabel,
  videoUrl,
  validateLocalVideoFile,
} from '../src/api.js'

test('buildClientRequestId 返回合法 UUID v4', () => {
  const id = buildClientRequestId()
  assert.ok(isUuid(id), `不是合法 UUID：${id}`)
  assert.equal(id[14], '4', '版本位应为 4')
  assert.ok(['8', '9', 'a', 'b'].includes(id[19]), '变体位应为 8/9/a/b')
  assert.notEqual(id, buildClientRequestId(), '两次生成不应相同')
})

test('formDataFromParams 包含全部字段', () => {
  const file = new File(['fake'], 'photo.png', { type: 'image/png' })
  const form = formDataFromParams({
    mode: 'image',
    prompt: '镜头缓慢推进',
    duration: 8,
    resolution: '720p',
    ratio: '9:16',
    generateAudio: true,
    clientRequestId: '550e8400-e29b-41d4-a716-446655440000',
    images: [{ file, role: 'first_frame' }],
    videoUrls: ['https://cdn.example.com/白模.mp4'],
    audioUrls: ['https://cdn.example.com/bgm.mp3'],
    returnLastFrame: true,
    webSearch: true,
  })
  assert.equal(form.get('mode'), 'image')
  assert.equal(form.get('prompt'), '镜头缓慢推进')
  assert.equal(form.get('duration'), '8')
  assert.equal(form.get('resolution'), '720p')
  assert.equal(form.get('ratio'), '9:16')
  assert.equal(form.get('generate_audio'), 'true')
  assert.equal(form.get('client_request_id'), '550e8400-e29b-41d4-a716-446655440000')
  assert.equal(form.get('image_roles'), 'first_frame')
  assert.equal(form.get('images').name, 'photo.png')
  assert.equal(form.get('video_urls'), 'https://cdn.example.com/白模.mp4')
  assert.equal(form.get('audio_urls'), 'https://cdn.example.com/bgm.mp3')
  assert.equal(form.get('return_last_frame'), 'true')
  assert.equal(form.get('web_search'), 'true')
  assert.equal(form.get('mock_outcome'), null)
})

test('formDataFromParams 多图角色与空素材过滤', () => {
  const a = new File(['a'], 'a.png', { type: 'image/png' })
  const b = new File(['b'], 'b.png', { type: 'image/png' })
  const form = formDataFromParams({
    mode: 'image',
    prompt: '',
    duration: 5,
    resolution: '480p',
    ratio: '16:9',
    generateAudio: false,
    clientRequestId: '550e8400-e29b-41d4-a716-446655440000',
    images: [
      { file: a, role: 'first_frame' },
      { file: b, role: 'last_frame' },
    ],
    videoUrls: ['', '  '],
    audioUrls: [],
  })
  assert.equal(form.get('image_roles'), 'first_frame,last_frame')
  assert.equal(form.getAll('images').length, 2)
  assert.equal(form.getAll('video_urls').length, 0, '空 URL 不应提交')
})

test('materialRefLabels 按内容顺序编号（官方提示词规则）', () => {
  const file = new File(['x'], 'x.png', { type: 'image/png' })
  const labels = materialRefLabels({
    images: [{ file, role: 'first_frame' }, { file, role: 'reference_image' }],
    videoUrls: ['https://cdn.example.com/v.mp4'],
    audioUrls: [],
  })
  assert.deepEqual(labels, [
    { kind: 'image', label: '图片', count: 2 },
    { kind: 'video', label: '视频', count: 1 },
    { kind: 'audio', label: '音频', count: 0 },
  ])
})

test('materialRefChips 每个素材一枚芯片且编号不越界', async () => {
  const { materialRefChips } = await import('../src/api.js')
  const file = new File(['x'], 'x.png', { type: 'image/png' })
  const chips = materialRefChips({
    images: [{ file, role: 'first_frame' }],
    videoUrls: ['https://cdn.example.com/v.mp4', 'https://cdn.example.com/v2.mp4'],
    audioUrls: [],
  })
  assert.deepEqual(chips, [
    { kind: 'image', label: '图片1' },
    { kind: 'video', label: '视频1' },
    { kind: 'video', label: '视频2' },
  ])
  // 空素材不产生芯片
  assert.deepEqual(materialRefChips({}), [])
})

test('lastFrameUrl 构建尾帧代理地址', () => {
  assert.equal(lastFrameUrl('mock-abc'), '/api/seedance/tasks/mock-abc/last-frame')
})

test('formDataFromParams 携带 mock_outcome 与空提示词', () => {
  const form = formDataFromParams({
    mode: 'text',
    prompt: '',
    duration: 5,
    resolution: '480p',
    ratio: '16:9',
    generateAudio: false,
    clientRequestId: '550e8400-e29b-41d4-a716-446655440000',
    mockOutcome: 'fail',
  })
  assert.equal(form.get('mock_outcome'), 'fail')
  assert.equal(form.get('generate_audio'), 'false')
  assert.equal(form.get('prompt'), '')
})

test('isUuid 识别合法与非法格式', () => {
  assert.ok(isUuid('550e8400-e29b-41d4-a716-446655440000'))
  assert.ok(isUuid('550e8400e29b41d4a716446655440000'))
  assert.ok(!isUuid('not-a-uuid'))
  assert.ok(!isUuid(''))
  assert.ok(!isUuid(null))
})

test('promptHistory 去重置顶且上限 20', () => {
  const history = ['a', 'b', 'c']
  const updated = addPromptHistory('b', history)
  assert.deepEqual(updated, ['b', 'a', 'c'], '重复项应移到最前')
  assert.deepEqual(history, ['a', 'b', 'c'], '不得修改原数组（不可变）')

  let many = []
  for (let i = 0; i < MAX_PROMPT_HISTORY; i++) many.push(`p${i}`)
  const capped = addPromptHistory('new', many)
  assert.equal(capped.length, MAX_PROMPT_HISTORY)
  assert.equal(capped[0], 'new')
  assert.ok(!capped.includes(`p${MAX_PROMPT_HISTORY - 1}`), '最旧的应被挤出')
})

test('loadPromptHistory 对损坏数据返回空数组', () => {
  const original = globalThis.localStorage
  globalThis.localStorage = {
    getItem: () => 'not-json',
  }
  try {
    assert.deepEqual(loadPromptHistory(), [])
  } finally {
    globalThis.localStorage = original
  }
})

test('statusLabel 覆盖全部状态', () => {
  assert.equal(statusLabel('submitted'), '已提交')
  assert.equal(statusLabel('queued'), '排队中')
  assert.equal(statusLabel('running'), '生成中')
  assert.equal(statusLabel('succeeded'), '生成成功')
  assert.equal(statusLabel('failed'), '生成失败')
  assert.equal(statusLabel('unknown'), '结果未知')
  assert.equal(statusLabel('weird'), '未知状态')
})

test('estimateCost 无价格时返回 null，有价格时按时长计算', () => {
  assert.equal(estimateCost(5, null), null)
  assert.equal(estimateCost(5, undefined), null)
  assert.equal(estimateCost(5, 0.5), 2.5)
  assert.equal(estimateCost(8, 0.25), 2)
})

test('videoUrl 构建代理下载地址', () => {
  assert.equal(videoUrl('mock-abc'), '/api/seedance/tasks/mock-abc/video')
})

test('parseDurationInput 接受 4~15 任意整数', () => {
  assert.equal(parseDurationInput(12), 12)
  assert.equal(parseDurationInput('12'), 12)
  assert.equal(parseDurationInput('7'), 7)
  assert.equal(parseDurationInput(4), 4)
  assert.equal(parseDurationInput(15), 15)
})

test('parseDurationInput 拒绝范围外与非法输入', () => {
  assert.equal(parseDurationInput(3), null)
  assert.equal(parseDurationInput(16), null)
  assert.equal(parseDurationInput('abc'), null)
  assert.equal(parseDurationInput(5.5), null)
  assert.equal(parseDurationInput(''), null)
  assert.equal(parseDurationInput(null), null)
})

test('shouldClearPendingKey 仅在成功/失败且键匹配时清除', () => {
  const crid = '550e8400-e29b-41d4-a716-446655440000'
  // 结果未知：绝不能清除幂等键（重试必须复用，防止重复扣费）
  assert.equal(shouldClearPendingKey({ status: 'unknown', client_request_id: crid }, crid), false)
  // 进行中：不清除
  assert.equal(shouldClearPendingKey({ status: 'running', client_request_id: crid }, crid), false)
  assert.equal(shouldClearPendingKey({ status: 'submitted', client_request_id: crid }, crid), false)
  // 终态且键匹配：清除
  assert.equal(shouldClearPendingKey({ status: 'succeeded', client_request_id: crid }, crid), true)
  assert.equal(shouldClearPendingKey({ status: 'failed', client_request_id: crid }, crid), true)
  // 终态但键不匹配（轮询的是别的任务）：不得清除
  assert.equal(shouldClearPendingKey({ status: 'succeeded', client_request_id: 'other-crid' }, crid), false)
})

test('isNotFoundError 识别 404 ApiError', async () => {
  const { ApiError } = await import('../src/api.js')
  assert.equal(isNotFoundError(new ApiError('任务不存在', { status: 404 })), true)
  assert.equal(isNotFoundError(new ApiError('网络错误', { status: 0 })), false)
  assert.equal(isNotFoundError(new Error('x')), false)
})

test('formDataFromParams 携带本地视频素材 ID', () => {
  const form = formDataFromParams({
    mode: 'edit',
    prompt: '复刻视频1',
    duration: 5,
    resolution: '720p',
    ratio: '9:16',
    generateAudio: false,
    clientRequestId: crypto.randomUUID(),
    videoUrls: ['https://cdn.example.com/a.mp4'],
    videoUploads: [{ uploadId: 'upload-1', status: 'ready' }],
  })
  assert.deepEqual(form.getAll('video_urls'), ['https://cdn.example.com/a.mp4'])
  assert.deepEqual(form.getAll('video_upload_ids'), ['upload-1'])
})

test('视频素材编号合并链接与本地上传', () => {
  assert.deepEqual(
    materialRefChips({
      videoUrls: ['https://cdn.example.com/a.mp4'],
      videoUploads: [
        { uploadId: 'u1', status: 'ready' },
        { localId: 'u2', status: 'uploading' },
      ],
    }),
    [
      { kind: 'video', label: '视频1' },
      { kind: 'video', label: '视频2' },
      { kind: 'video', label: '视频3' },
    ],
  )
})

test('validateLocalVideoFile 拒绝不支持格式与超大文件', () => {
  assert.equal(
    validateLocalVideoFile(new File(['x'], 'a.txt', { type: 'text/plain' })),
    '仅支持 MP4 或 MOV 视频',
  )
  const oversized = new File(['x'], 'large.mp4', { type: 'video/mp4' })
  Object.defineProperty(oversized, 'size', { value: 51 * 1024 * 1024 })
  assert.match(validateLocalVideoFile(oversized), /50MB/)
  assert.equal(validateLocalVideoFile(new File(['x'], 'a.mp4', { type: 'video/mp4' })), null)
})
