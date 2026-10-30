export function createAmapLoader({ documentRef = document, windowRef = window } = {}) {
  let pending = null
  return function loadAmap(jsKey) {
    if (windowRef.AMap) return Promise.resolve(windowRef.AMap)
    if (!jsKey) return Promise.reject(new Error('VITE_AMAP_JS_KEY is not configured'))
    if (pending) return pending
    pending = new Promise((resolve, reject) => {
      const script = documentRef.createElement('script')
      script.async = true
      script.src = `https://webapi.amap.com/maps?v=2.0&key=${encodeURIComponent(jsKey)}`
      script.onload = () => windowRef.AMap
        ? resolve(windowRef.AMap)
        : reject(new Error('高德地图 SDK 未正确初始化'))
      script.onerror = () => reject(new Error('高德地图 SDK 加载失败'))
      documentRef.head.appendChild(script)
    }).catch(error => {
      pending = null
      throw error
    })
    return pending
  }
}

export const loadAmap = typeof document === 'undefined'
  ? () => Promise.reject(new Error('高德地图只能在浏览器中加载'))
  : createAmapLoader()
