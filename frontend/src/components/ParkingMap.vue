<template>
  <div class="parking-map-shell">
    <div ref="container" class="parking-map" aria-label="医院和停车场地图"></div>
    <p v-if="error" class="map-error">{{ error }}。候选列表仍可使用。</p>
  </div>
</template>

<script setup>
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { loadAmap } from '../lib/amap-loader'

const props = defineProps({
  hospital: { type: Object, default: null },
  candidates: { type: Array, default: () => [] },
  selectedParkingId: { type: String, default: '' },
  route: { type: Object, default: null },
})
const emit = defineEmits(['select-parking', 'map-error'])
const container = ref(null)
const error = ref('')
let AMap = null
let map = null
let overlays = []

function lngLat(point) {
  return point ? [point.longitude, point.latitude] : null
}

function clearOverlays() {
  if (map && overlays.length) map.remove(overlays)
  overlays = []
}

function render() {
  if (!map || !AMap) return
  clearOverlays()
  if (props.hospital?.location) {
    overlays.push(new AMap.Marker({
      position: lngLat(props.hospital.entrance_location || props.hospital.location),
      title: props.hospital.name,
      label: { content: `医院：${props.hospital.name}`, direction: 'top' },
    }))
  }
  for (const item of props.candidates) {
    const marker = new AMap.Marker({
      position: lngLat(item.entrance_location || item.location),
      title: item.name,
      label: item.parking_id === props.selectedParkingId
        ? { content: `已选：${item.name}`, direction: 'top' }
        : undefined,
    })
    marker.on('click', () => emit('select-parking', item))
    overlays.push(marker)
  }
  const path = (props.route?.polyline || []).map(lngLat).filter(Boolean)
  if (path.length > 1) {
    overlays.push(new AMap.Polyline({ path, strokeColor: '#5964dd', strokeWeight: 7 }))
  }
  if (overlays.length) {
    map.add(overlays)
    map.setFitView(overlays, false, [60, 60, 60, 60], 16)
  }
}

onMounted(async () => {
  try {
    AMap = await loadAmap(import.meta.env.VITE_AMAP_JS_KEY)
    map = new AMap.Map(container.value, { zoom: 14, viewMode: '2D' })
    if (AMap.Scale) map.addControl(new AMap.Scale())
    render()
  } catch (loadError) {
    error.value = loadError.message
    emit('map-error', loadError.message)
  }
})

watch(() => [props.hospital, props.candidates, props.selectedParkingId, props.route], render, { deep: true })
onBeforeUnmount(() => {
  clearOverlays()
  map?.destroy()
})
</script>
