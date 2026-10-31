function normalizedId(value, label, { rejectSimulation = true } = {}) {
  if (typeof value !== 'string') {
    throw new Error(`${label}必须是字符串`)
  }
  const normalized = value.trim()
  if (!normalized || normalized !== value) {
    throw new Error(`${label}不能为空且不能包含首尾空格`)
  }
  if (rejectSimulation && normalized.startsWith('SIM-')) {
    throw new Error(`${label}不能使用模拟标识`)
  }
  return normalized
}

export function hospitalSelectionEvent(selection, hospital) {
  if (!selection || typeof selection !== 'object' || Array.isArray(selection)) {
    throw new Error('院区选择信息无效')
  }
  const queryId = normalizedId(selection.query_id, '查询标识', { rejectSimulation: false })
  if (!Number.isSafeInteger(selection.candidate_version) || selection.candidate_version < 1) {
    throw new Error('候选版本必须是大于等于 1 的安全整数')
  }
  if (!hospital || typeof hospital !== 'object' || Array.isArray(hospital)) {
    throw new Error('院区标识不能为空')
  }
  const hospitalId = normalizedId(hospital.hospital_id, '院区标识')
  return {
    kind: 'map_hospital_select',
    query_id: queryId,
    candidate_version: selection.candidate_version,
    hospital_id: hospitalId,
  }
}

export function mapHandoff(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    throw new Error('地图接力数据必须是对象')
  }
  if (!value.hospital || value.hospital.data_tier !== 'map_poi') {
    throw new Error('院区必须来自真实地图')
  }
  normalizedId(value.hospital.hospital_id, '院区标识')
  if (!Array.isArray(value.candidates)) {
    throw new Error('停车候选列表必须是数组')
  }

  const candidates = value.candidates.map((candidate) => {
    if (!candidate || typeof candidate !== 'object' || Array.isArray(candidate) || candidate.data_tier !== 'map_poi') {
      throw new Error('停车候选必须来自真实地图')
    }
    normalizedId(candidate.parking_id, '停车场标识')
    return {
      ...candidate,
      available_spaces: candidate.available_spaces ?? null,
      wheelchair_accessible: candidate.wheelchair_accessible ?? null,
    }
  })

  return { mode: 'real', hospital: value.hospital, candidates }
}
