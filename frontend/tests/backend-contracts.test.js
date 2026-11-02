import test from 'node:test'
import assert from 'node:assert/strict'

import { normalizeChatResponse } from '../src/lib/backends.js'


test('chat response normalizes retrieval and safety telemetry', () => {
  const result = normalizeChatResponse('careguide', {
    retrieval: {
      mode: 'hybrid', lexical_count: 20, dense_count: 20,
      fused_count: 27, reranked: true, filtered_count: 7,
      final_count: 3, degraded_reason: null
    },
    safety: { disposition: 'non_emergency_information', red_flags: [] },
    recommended_department: '神经内科'
  })

  assert.deepEqual(result.retrieval, {
    mode: 'hybrid', lexicalCount: 20, denseCount: 20,
    fusedCount: 27, reranked: true, filteredCount: 7,
    finalCount: 3, degradedReason: null
  })
  assert.equal(result.safety.disposition, 'non_emergency_information')
  assert.deepEqual(result.safety.redFlags, [])
  assert.equal(result.recommendedDepartment, '神经内科')
})


test('missing retrieval telemetry has an explicit not-used state', () => {
  const result = normalizeChatResponse('careguide', {})

  assert.equal(result.retrieval.mode, 'not_used')
  assert.equal(result.retrieval.reranked, false)
  assert.equal(result.retrieval.finalCount, 0)
  assert.equal(result.safety.disposition, 'not_assessed')
})
