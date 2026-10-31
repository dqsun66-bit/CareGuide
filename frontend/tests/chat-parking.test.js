import test from 'node:test'
import assert from 'node:assert/strict'

import {
  hospitalSelectionEvent,
  mapHandoff,
} from '../src/lib/chat-parking.js'
import {
  buildChatPayload,
  normalizeChatResponse,
} from '../src/lib/backends.js'

const selection = { query_id: 'query-1', candidate_version: 2 }
const hospital = {
  hospital_id: 'hospital-1',
  name: '泰州市人民医院',
  address: '医药高新区太湖路',
  location: { longitude: 119.9, latitude: 32.4 },
  available_spaces: 20,
  data_tier: 'map_poi',
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
  assert.throws(() => hospitalSelectionEvent({ ...selection, candidate_version: 0 }, hospital), /候选版本/)
  assert.throws(() => hospitalSelectionEvent({ ...selection, candidate_version: 1.5 }, hospital), /候选版本/)
  assert.throws(() => hospitalSelectionEvent(selection, { ...hospital, hospital_id: '' }), /院区标识/)
  assert.throws(() => hospitalSelectionEvent(selection, { ...hospital, hospital_id: 'SIM-H1' }), /模拟/)
})

test('map handoff preserves real map fields and explicitly retains unknown parking truth', () => {
  const value = {
    hospital,
    candidates: [{
      parking_id: 'parking-1',
      name: '医院停车场',
      data_tier: 'map_poi',
      source_provider: 'amap',
      distance_m: 120,
    }],
  }
  const before = structuredClone(value)

  assert.deepEqual(mapHandoff(value), {
    mode: 'real',
    hospital,
    candidates: [{
      parking_id: 'parking-1',
      name: '医院停车场',
      data_tier: 'map_poi',
      source_provider: 'amap',
      distance_m: 120,
      available_spaces: null,
      wheelchair_accessible: null,
    }],
  })
  assert.deepEqual(value, before)
})

test('map handoff preserves known false and zero truth values', () => {
  const result = mapHandoff({
    hospital,
    candidates: [{
      parking_id: 'parking-1',
      data_tier: 'map_poi',
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
  assert.throws(() => mapHandoff({ hospital: { ...hospital, data_tier: 'simulation' }, candidates: [] }), /真实地图/)
  assert.throws(() => mapHandoff({ hospital, candidates: [{ parking_id: 'P1', data_tier: 'simulation' }] }), /真实地图/)
  assert.throws(() => mapHandoff({ hospital, candidates: [{ parking_id: '', data_tier: 'map_poi' }] }), /停车场标识/)
  assert.throws(() => mapHandoff({ hospital, candidates: [{ parking_id: 'SIM-P1', data_tier: 'map_poi' }] }), /模拟/)
})

test('chat payload serializes workflow and map events into independent fields', () => {
  const settings = { userId: 'u1', conversationId: 'c1' }
  const workflowEvent = { kind: 'answer', event_id: 'workflow-event' }
  const mapEvent = { kind: 'map_hospital_select', hospital_id: 'hospital-1' }

  assert.deepEqual(buildChatPayload('careguide', settings, '继续', workflowEvent), {
    message: '继续', user_id: 'u1', conv_id: 'c1', workflow_event: workflowEvent,
  })
  assert.deepEqual(buildChatPayload('careguide', settings, '查看停车', undefined, mapEvent), {
    message: '查看停车', user_id: 'u1', conv_id: 'c1', map_event: mapEvent,
  })
  assert.deepEqual(buildChatPayload('careguide', settings, '继续', workflowEvent, mapEvent), {
    message: '继续', user_id: 'u1', conv_id: 'c1', workflow_event: workflowEvent, map_event: mapEvent,
  })
})

test('chat response normalizes snake and camel map contracts and defaults to null', () => {
  const selection = { query_id: 'q1' }
  const parking = { candidates: [] }

  assert.equal(normalizeChatResponse('careguide', {}).mapSelection, null)
  assert.equal(normalizeChatResponse('careguide', {}).mapParking, null)
  assert.equal(normalizeChatResponse('careguide', { map_selection: selection }).mapSelection, selection)
  assert.equal(normalizeChatResponse('careguide', { mapParking: parking }).mapParking, parking)
})
