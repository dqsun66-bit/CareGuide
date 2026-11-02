import test from 'node:test'
import assert from 'node:assert/strict'

import {
  hospitalSelectionEvent,
  mapHandoff,
} from '../src/lib/chat-parking.js'
import {
  buildChatPayload,
  normalizeChatResponse,
  requestChat,
  requestChatJson,
} from '../src/lib/backends.js'

const selection = { query_id: 'query-1', candidate_version: 2 }
const hospital = {
  hospital_id: 'hospital-1',
  name: '泰州市人民医院',
  address: '医药高新区太湖路',
  location: { longitude: 119.9, latitude: 32.4 },
  available_spaces: 20,
  data_tier: 'map_poi',
  source_provider: 'amap',
}

test('hospital selection emits only the version-bound hospital identity', () => {
  const event = hospitalSelectionEvent(selection, hospital)

  assert.deepEqual(event, {
    kind: 'map_hospital_select',
    query_id: 'query-1',
    candidate_version: 2,
    hospital_id: 'hospital-1',
  })
  assert.equal('location' in event, false)
  assert.equal('coordinates' in event, false)
  assert.equal('address' in event, false)
  assert.equal('available_spaces' in event, false)
})

test('hospital selection rejects missing identity, invalid versions, and simulated hospitals', () => {
  assert.throws(() => hospitalSelectionEvent(null, hospital), /选择信息/)
  assert.throws(() => hospitalSelectionEvent({ ...selection, query_id: ' ' }, hospital), /查询标识/)
  assert.throws(() => hospitalSelectionEvent({ ...selection, query_id: ' query-1' }, hospital), /查询标识/)
  assert.throws(() => hospitalSelectionEvent({ ...selection, candidate_version: 0 }, hospital), /候选版本/)
  assert.throws(() => hospitalSelectionEvent({ ...selection, candidate_version: 1.5 }, hospital), /候选版本/)
  assert.throws(() => hospitalSelectionEvent(selection, { ...hospital, hospital_id: undefined }), /院区标识/)
  assert.throws(() => hospitalSelectionEvent(selection, { ...hospital, hospital_id: '' }), /院区标识/)
  assert.throws(() => hospitalSelectionEvent(selection, { ...hospital, hospital_id: ' hospital-1' }), /院区标识/)
  assert.throws(() => hospitalSelectionEvent(selection, { ...hospital, hospital_id: 'SIM-H1' }), /模拟/)
  assert.throws(() => hospitalSelectionEvent(selection, { ...hospital, hospital_id: ' SIM-H1' }), /院区标识/)
})

test('map handoff preserves real map fields and explicitly retains unknown parking truth', () => {
  const value = {
    source_provider: 'amap',
    hospital: {
      ...hospital,
      entrance_location: { longitude: 119.91, latitude: 32.41 },
    },
    candidates: [{
      parking_id: 'parking-1',
      name: '医院停车场',
      data_tier: 'map_poi',
      source_provider: 'amap',
      distance_m: 120,
      location: { longitude: 119.92, latitude: 32.42 },
    }],
  }
  const before = structuredClone(value)
  const result = mapHandoff(value)

  assert.deepEqual(result, {
    mode: 'real',
    hospital: value.hospital,
    candidates: [{
      parking_id: 'parking-1',
      name: '医院停车场',
      data_tier: 'map_poi',
      source_provider: 'amap',
      distance_m: 120,
      location: { longitude: 119.92, latitude: 32.42 },
      available_spaces: null,
      wheelchair_accessible: null,
    }],
  })
  assert.notStrictEqual(result.hospital, value.hospital)
  assert.notStrictEqual(result.hospital.location, value.hospital.location)
  assert.notStrictEqual(result.hospital.entrance_location, value.hospital.entrance_location)
  assert.notStrictEqual(result.candidates[0], value.candidates[0])
  assert.notStrictEqual(result.candidates[0].location, value.candidates[0].location)
  result.hospital.name = '已修改院区'
  result.hospital.location.longitude = 0
  result.candidates[0].name = '已修改停车场'
  result.candidates[0].location.latitude = 0
  assert.deepEqual(value, before)
})

test('map handoff preserves known false and zero truth values', () => {
  const result = mapHandoff({
    source_provider: 'amap',
    hospital,
    candidates: [{
      parking_id: 'parking-1',
      data_tier: 'map_poi',
      source_provider: 'amap',
      available_spaces: 0,
      wheelchair_accessible: false,
    }],
  })

  assert.equal(result.candidates[0].available_spaces, 0)
  assert.equal(result.candidates[0].wheelchair_accessible, false)
})

test('map handoff rejects malformed, non-map, simulated, and unidentified values', () => {
  assert.throws(() => mapHandoff(null), /地图接力数据/)
  assert.throws(() => mapHandoff({ hospital, candidates: null }), /停车候选列表/)
  assert.throws(() => mapHandoff({ source_provider: 'evil', hospital, candidates: [] }), /高德/)
  assert.throws(() => mapHandoff({ source_provider: 'amap', hospital: { ...hospital, source_provider: 'evil' }, candidates: [] }), /高德/)
  assert.throws(() => mapHandoff({ source_provider: 'amap', hospital, candidates: [{ parking_id: 'P1', data_tier: 'map_poi', source_provider: 'evil' }] }), /高德/)
  assert.throws(() => mapHandoff({ hospital: { ...hospital, hospital_id: undefined }, candidates: [] }), /院区标识/)
  assert.throws(() => mapHandoff({ hospital: { ...hospital, hospital_id: '' }, candidates: [] }), /院区标识/)
  assert.throws(() => mapHandoff({ hospital: { ...hospital, hospital_id: ' ' }, candidates: [] }), /院区标识/)
  assert.throws(() => mapHandoff({ hospital: { ...hospital, hospital_id: 'SIM-H1' }, candidates: [] }), /模拟/)
  assert.throws(() => mapHandoff({ hospital: { ...hospital, hospital_id: ' SIM-H1' }, candidates: [] }), /院区标识/)
  assert.throws(() => mapHandoff({ hospital: { ...hospital, data_tier: 'simulation' }, candidates: [] }), /真实地图/)
  assert.throws(() => mapHandoff({ hospital, candidates: [{ parking_id: 'P1', data_tier: 'simulation' }] }), /真实地图/)
  assert.throws(() => mapHandoff({ hospital, candidates: [{ parking_id: '', data_tier: 'map_poi' }] }), /停车场标识/)
  assert.throws(() => mapHandoff({ hospital, candidates: [{ parking_id: 'SIM-P1', data_tier: 'map_poi' }] }), /模拟/)
  assert.throws(() => mapHandoff({ hospital, candidates: [{ parking_id: ' SIM-P1', data_tier: 'map_poi' }] }), /停车场标识/)
})

test('chat payload serializes either workflow or map event and rejects ambiguous dual events', () => {
  const settings = { userId: 'u1', conversationId: 'c1' }
  const workflowEvent = { kind: 'answer', event_id: 'workflow-event' }
  const mapEvent = { kind: 'map_hospital_select', hospital_id: 'hospital-1' }

  assert.deepEqual(buildChatPayload('careguide', settings, '继续', workflowEvent), {
    message: '继续', user_id: 'u1', conv_id: 'c1', workflow_event: workflowEvent,
  })
  assert.deepEqual(buildChatPayload('careguide', settings, '查看停车', undefined, mapEvent), {
    message: '查看停车', user_id: 'u1', conv_id: 'c1', map_event: mapEvent,
  })
  assert.throws(
    () => buildChatPayload('careguide', settings, '继续', workflowEvent, mapEvent),
    /工作流事件.*地图事件|地图事件.*工作流事件/,
  )
})

test('JSON and SSE chat adapters put map events in the final parameter and actual request body', async () => {
  const originalFetch = globalThis.fetch
  const originalWindow = globalThis.window
  const calls = []
  const mapEvent = { kind: 'map_hospital_select', hospital_id: 'hospital-1' }
  const workflowEvent = { kind: 'answer', event_id: 'workflow-event' }
  globalThis.window = { setTimeout, clearTimeout }
  globalThis.fetch = async (url, options) => {
    calls.push({ url, body: JSON.parse(options.body) })
    if (String(url).endsWith('/chat/stream')) {
      const text = sseFrame(0, 'session', { conversation_id: 'c1' }) +
        sseFrame(1, 'result', { response: '完成', conversation_id: 'c1' })
      return sseResponse(text)
    }
    return new Response(JSON.stringify({ response: '完成', conversation_id: 'c1' }), {
      headers: { 'Content-Type': 'application/json' },
    })
  }

  try {
    const settings = { endpoint: '/api', userId: 'u1', conversationId: 'c1' }
    await requestChatJson('careguide', settings, '工作流', undefined, workflowEvent)
    await requestChatJson('careguide', settings, '地图', undefined, undefined, mapEvent)
    await requestChat('careguide', settings, '流式地图', undefined, undefined, undefined, mapEvent)

    assert.deepEqual(calls.map(({ url, body }) => ({ url, body })), [
      { url: '/api/chat', body: { message: '工作流', user_id: 'u1', conv_id: 'c1', workflow_event: workflowEvent } },
      { url: '/api/chat', body: { message: '地图', user_id: 'u1', conv_id: 'c1', map_event: mapEvent } },
      { url: '/api/chat/stream', body: { message: '流式地图', user_id: 'u1', conv_id: 'c1', map_event: mapEvent } },
    ])
  } finally {
    globalThis.fetch = originalFetch
    if (originalWindow === undefined) delete globalThis.window
    else globalThis.window = originalWindow
  }
})

test('chat adapters reject dual workflow and map events before making a request', async () => {
  const originalFetch = globalThis.fetch
  let calls = 0
  globalThis.fetch = async () => { calls += 1; throw new Error('不应请求') }
  const settings = { endpoint: '/api', userId: 'u1' }
  const workflowEvent = { kind: 'answer' }
  const mapEvent = { kind: 'map_hospital_select' }

  try {
    await assert.rejects(
      requestChatJson('careguide', settings, '冲突', undefined, workflowEvent, mapEvent),
      /工作流事件.*地图事件|地图事件.*工作流事件/,
    )
    await assert.rejects(
      requestChat('careguide', settings, '冲突', undefined, workflowEvent, undefined, mapEvent),
      /工作流事件.*地图事件|地图事件.*工作流事件/,
    )
    assert.equal(calls, 0)
  } finally {
    globalThis.fetch = originalFetch
  }
})

test('chat response normalizes snake and camel map contracts and defaults to null', () => {
  const selection = { query_id: 'q1' }
  const parking = { candidates: [] }

  assert.equal(normalizeChatResponse('careguide', {}).mapSelection, null)
  assert.equal(normalizeChatResponse('careguide', {}).mapParking, null)
  assert.deepEqual(normalizeChatResponse('careguide', { map_selection: selection }).mapSelection, {
    ...selection, hospitals: [],
  })
  assert.deepEqual(normalizeChatResponse('careguide', { mapParking: parking }).mapParking, {
    hospital: {}, candidates: [],
  })
})

test('chat response makes malformed map response shapes safe for UI consumers', () => {
  const malformed = normalizeChatResponse('careguide', {
    map_selection: 'bad',
    map_parking: { hospital: 'bad', candidates: { parking_id: 'P1' } },
  })
  assert.equal(malformed.mapSelection, null)
  assert.deepEqual(malformed.mapParking, { hospital: {}, candidates: [] })

  const selection = normalizeChatResponse('careguide', {
    mapSelection: { query_id: 'q1', hospitals: null },
  }).mapSelection
  assert.deepEqual(selection, { query_id: 'q1', hospitals: [] })
})

function sseFrame(id, event, data) {
  return `id: ${id}\nevent: ${event}\ndata: ${JSON.stringify(data)}\n\n`
}

function sseResponse(text) {
  const bytes = new TextEncoder().encode(text)
  return new Response(new ReadableStream({
    start(controller) {
      controller.enqueue(bytes)
      controller.close()
    },
  }), { headers: { 'Content-Type': 'text/event-stream' } })
}
