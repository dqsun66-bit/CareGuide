const DEMO_HOSPITAL_ID = 'tzph-taihu-demo'

export async function requestParkingReplan(requester, options) {
  const payload = await requester('/demo/parking/replan', {
    hospital_id: DEMO_HOSPITAL_ID,
    max_walking_distance_m: options.maxWalkingDistanceM,
    requires_wheelchair_access: options.requiresWheelchairAccess,
    event: {
      parking_lot_id: options.eventLotId,
      available_spaces: options.availableSpaces,
    },
  })
  const candidates = payload?.after?.candidates || []
  if (payload?.simulation !== true || candidates.some(item => item.data_tier === 'map_poi')) {
    throw new Error('Simulation mode received real map data')
  }
  return payload
}

export function parkingTruthLabels(candidate) {
  return {
    availability: candidate.available_spaces == null
      ? '实时车位未知'
      : `剩余 ${candidate.available_spaces} 个车位`,
    accessibility: candidate.wheelchair_accessible == null
      ? '无障碍通行未核实'
      : candidate.wheelchair_accessible ? '无障碍通行已核实' : '不支持无障碍通行',
  }
}

export function buildParkingToHospitalRoute(hospital, parking) {
  return {
    origin: parking.entrance_location || parking.location,
    destination: hospital.entrance_location || hospital.location,
    mode: 'walking',
  }
}

function requireRealPayload(payload) {
  if (!payload || payload.simulation !== false) {
    throw new Error('Real map mode received simulation data')
  }
  return payload
}

export async function searchMapHospitals(requester, query, city = '泰州') {
  const params = new URLSearchParams({ q: query, city })
  const payload = requireRealPayload(await requester(`/map/hospitals/search?${params}`))
  return payload.hospitals || []
}

export async function searchMapParking(requester, hospitalId, radiusM = 1000) {
  const payload = requireRealPayload(await requester(
    `/map/hospitals/${encodeURIComponent(hospitalId)}/parking?radius_m=${encodeURIComponent(radiusM)}`,
  ))
  const candidates = payload.candidates || []
  if (candidates.some(item => String(item.parking_id || '').startsWith('SIM-'))) {
    throw new Error('Real map mode received simulation data')
  }
  return candidates.map(item => ({
    ...item,
    available_spaces: item.available_spaces ?? null,
    wheelchair_accessible: item.wheelchair_accessible ?? null,
  }))
}

export async function requestMapRoute(requester, routeRequest) {
  const payload = requireRealPayload(await requester('/map/routes', routeRequest))
  return payload.route
}
