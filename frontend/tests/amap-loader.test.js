import test from 'node:test'
import assert from 'node:assert/strict'

import { createAmapLoader } from '../src/lib/amap-loader.js'


test('Amap loader rejects a missing JS key without adding a script', async () => {
  const appended = []
  const loader = createAmapLoader({
    documentRef: { head: { appendChild: node => appended.push(node) }, createElement: () => ({}) },
    windowRef: {},
  })
  await assert.rejects(() => loader(''), /VITE_AMAP_JS_KEY/)
  assert.equal(appended.length, 0)
})

test('Amap loader is single-flight and resolves the loaded SDK', async () => {
  const windowRef = {}
  let appended = 0
  const documentRef = {
    createElement: () => ({}),
    head: { appendChild(script) {
      appended += 1
      windowRef.AMap = { Map: class {} }
      queueMicrotask(() => script.onload())
    } },
  }
  const loader = createAmapLoader({ documentRef, windowRef })
  const [first, second] = await Promise.all([loader('public-key'), loader('public-key')])
  assert.equal(appended, 1)
  assert.equal(first, second)
})

test('Amap loader requests the scale plugin without exposing key elsewhere', async () => {
  const windowRef = {}
  let source = ''
  const loader = createAmapLoader({
    windowRef,
    documentRef: {
      createElement: () => ({}),
      head: { appendChild(script) {
        source = script.src
        windowRef.AMap = { Map: class {} }
        queueMicrotask(() => script.onload())
      } },
    },
  })
  await loader('public-key')
  assert.match(source, /plugin=AMap\.Scale/)
})
