function nonEmptyId(value) {
  return typeof value === 'string' && value.trim().length > 0
}

function simulatedId(value) {
  return value.startsWith('SIM-')
}

export function hospitalSelectionEvent(selection, hospital) {
  if (!selection || typeof selection !== 'object' || Array.isArray(selection)) {
    throw new Error('院区选择信息无效')
  }
  if (!nonEmptyId(selection.query_id)) {
    throw new Error('查询标识不能为空')
  }
  if (!Number.isSafeInteger(selection.candidate_version) || selection.candidate_version < 1) {
    throw new Error('候选版本必须是大于等于 1 的安全整数')
  }
  if (!hospital || typeof hospital !== 'object' || Array.isArray(hospital) || !nonEmptyId(hospital.hospital_id)) {
    throw new Error('院区标识不能为空')
  }
  if (simulatedId(hospital.hospital_id)) {
    throw new Error('不能选择模拟院区')
  }
  return {
    kind: 'map_hospital_select',
    query_id: selection.query_id,
    candidate_version: selection.candidate_version,
    hospital_id: hospital.hospital_id,
  }
}

export function mapHandoff(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    throw new Error('地图接力数据必须是对象')
  }
  if (!value.hospital || value.hospital.data_tier !== 'map_poi') {
    throw new Error('院区必须来自真实地图')
  }
  if (!Array.isArray(value.candidates)) {
    throw new Error('停车候选列表必须是数组')
  }

  const candidates = value.candidates.map((candidate) => {
    if (!candidate || typeof candidate !== 'object' || Array.isArray(candidate) || candidate.data_tier !== 'map_poi') {
      throw new Error('停车候选必须来自真实地图')
    }
    if (!nonEmptyId(candidate.parking_id)) {
      throw new Error('停车场标识不能为空')
    }
    if (simulatedId(candidate.parking_id)) {
      throw new Error('不能使用模拟停车场')
    }
    return {
      ...candidate,
      available_spaces: candidate.available_spaces ?? null,
      wheelchair_accessible: candidate.wheelchair_accessible ?? null,
    }
  })

  return { mode: 'real', hospital: value.hospital, candidates }
}
