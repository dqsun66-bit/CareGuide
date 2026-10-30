import test from 'node:test'
import assert from 'node:assert/strict'

import {
  buildParkingToHospitalRoute,
  parkingTruthLabels,
  requestMapRoute,
  requestParkingReplan,
  searchMapHospitals,
  searchMapParking,
} from '../src/lib/parking.js'


test('parking demo calls only the explicit simulation endpoint and preserves constraints', async () => {
  const calls = []
  const requester = async (path, body) => {
    calls.push({ path, body })
    return { simulation: true, before: { selected_lot_id: 'SIM-A' }, after: { selected_lot_id: 'SIM-B' } }
  }

  const result = await requestParkingReplan(requester, {
    maxWalkingDistanceM: 200,
    requiresWheelchairAccess: true,
    eventLotId: 'SIM-A',
    availableSpaces: 0,
  })

  assert.deepEqual(calls, [{
    path: '/demo/parking/replan',
    body: {
      hospital_id: 'tzph-taihu-demo',
      max_walking_distance_m: 200,
      requires_wheelchair_access: true,
      event: { parking_lot_id: 'SIM-A', available_spaces: 0 },
    },
  }])
  assert.equal(result.after.selected_lot_id, 'SIM-B')
})

test('real map requests retain unknown truth fields and never accept simulation', async () => {
  const calls = []
  const requester = async (path, body) => {
    calls.push({ path, body })
    if (path.startsWith('/map/hospitals/search')) {
      return { simulation: false, hospitals: [{ hospital_id: 'H1', name: '总院' }] }
    }
    return { simulation: false, candidates: [{
      parking_id: 'P1', name: '总院停车场', available_spaces: null,
      wheelchair_accessible: null, data_tier: 'map_poi', source_provider: 'amap',
    }] }
  }

  const hospitals = await searchMapHospitals(requester, '泰州市人民医院', '泰州')
  const parking = await searchMapParking(requester, 'H1', 1000)

  assert.equal(hospitals[0].hospital_id, 'H1')
  assert.equal(parking[0].available_spaces, null)
  assert.equal(parking[0].wheelchair_accessible, null)
  assert.deepEqual(calls, [
    { path: '/map/hospitals/search?q=%E6%B3%B0%E5%B7%9E%E5%B8%82%E4%BA%BA%E6%B0%91%E5%8C%BB%E9%99%A2&city=%E6%B3%B0%E5%B7%9E', body: undefined },
    { path: '/map/hospitals/H1/parking?radius_m=1000', body: undefined },
  ])
})

test('real map mode rejects simulated candidates', async () => {
  await assert.rejects(
    () => searchMapParking(async () => ({ simulation: true, candidates: [{ parking_id: 'SIM-A' }] }), 'H1'),
    /simulation data/i,
  )
})

test('route request uses the read-only map route endpoint', async () => {
  const calls = []
  const route = await requestMapRoute(async (path, body) => {
    calls.push({ path, body })
    return { simulation: false, route: { mode: 'walking', wheelchair_verified: false } }
  }, {
    origin: { longitude: 119.923, latitude: 32.455 },
    destination: { longitude: 119.9228, latitude: 32.4552 },
    mode: 'walking',
  })
  assert.equal(route.wheelchair_verified, false)
  assert.equal(calls[0].path, '/map/routes')
})

test('simulation mode rejects map POI data and real labels preserve unknown states', async () => {
  await assert.rejects(
    () => requestParkingReplan(async () => ({
      simulation: true,
      after: { candidates: [{ id: 'P1', data_tier: 'map_poi' }] },
    }), {
      maxWalkingDistanceM: 200,
      requiresWheelchairAccess: true,
      eventLotId: 'SIM-A',
      availableSpaces: 0,
    }),
    /map data/i,
  )

  assert.deepEqual(parkingTruthLabels({
    available_spaces: null,
    wheelchair_accessible: null,
  }), {
    availability: '实时车位未知',
    accessibility: '无障碍通行未核实',
  })
})

test('walking route starts at parking and ends at the hospital entrance', () => {
  const hospital = {
    location: { longitude: 119.9, latitude: 32.4 },
    entrance_location: { longitude: 119.91, latitude: 32.41 },
  }
  const parking = {
    location: { longitude: 119.8, latitude: 32.3 },
    entrance_location: { longitude: 119.81, latitude: 32.31 },
  }
  assert.deepEqual(buildParkingToHospitalRoute(hospital, parking), {
    origin: parking.entrance_location,
    destination: hospital.entrance_location,
    mode: 'walking',
  })
})
