<template>
  <div class="registration-entry">
    <button type="button" @click="copyToken">复制医院小程序口令</button>
    <p v-if="status" role="status">{{ status }}</p>
    <input v-if="manual" aria-label="医院小程序口令" :value="registrationToken" readonly @focus="$event.target.select()" />
  </div>
</template>

<script setup>
import { ref } from 'vue'
import { registrationToken } from '../lib/registration-entry.js'

const status = ref('')
const manual = ref(false)
const instructions = '粘贴到微信文件传输助手并发送，点击消息里的蓝色小程序链接进入医院小程序。今天就诊选「当日挂号」，提前就诊选「预约挂号」。'

async function copyToken() {
  let copied = false
  try {
    await navigator.clipboard.writeText(registrationToken)
    copied = true
  } catch {
    // Local-network HTTP pages may lack the asynchronous clipboard API.
    const field = document.createElement('textarea')
    const trigger = document.activeElement
    field.value = registrationToken
    field.style.cssText = 'position:fixed;left:-9999px;top:0'
    document.body.append(field)
    try {
      field.select()
      copied = document.execCommand('copy')
    } catch {
      copied = false
    } finally {
      field.remove()
      trigger?.focus()
    }
  }
  manual.value = !copied
  status.value = (copied ? '已复制。' : '请手动复制下方口令。') + instructions
}
</script>

<style scoped>
.registration-entry { margin: .75rem 0; }
.registration-entry input { width: 100%; }
</style>
