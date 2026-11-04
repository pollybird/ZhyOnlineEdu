<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { usePluginStore } from '../stores/plugin'

/**
 * 插件页面宿主（M3.5）：路由 `/plugin/<code>/<subpath>`。
 *
 * 页面由插件 ESM 模块提供（`pages[subpath]`），挂载到本组件容器；
 * 模块返回的函数在离开路由时执行，避免插件残留定时器/监听。
 */
const route = useRoute()
const store = usePluginStore()
const host = ref<HTMLElement | null>(null)
const errorMessage = ref('')
let cleanup: (() => void) | null = null

function currentSubpath(): string {
  const raw = route.params.subpath
  if (Array.isArray(raw)) {
    return raw.join('/')
  }
  return typeof raw === 'string' ? raw : ''
}

async function mountPage(): Promise<void> {
  cleanup?.()
  cleanup = null
  errorMessage.value = ''
  host.value?.replaceChildren()
  const code = String(route.params.code ?? '')
  const subpath = currentSubpath()
  await store.ensureLoaded()
  const mount = store.page(code, subpath)
  if (mount === null) {
    errorMessage.value = `插件「${code}」未提供页面「${subpath || 'index'}」，或前端入口加载失败。`
    return
  }
  if (host.value === null) {
    return
  }
  try {
    const result = mount(host.value, store.context(code))
    cleanup = typeof result === 'function' ? result : null
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '插件页面加载失败'
  }
}

onMounted(() => {
  void mountPage()
})

watch(
  () => [route.params.code, route.params.subpath],
  () => {
    void mountPage()
  },
  { flush: 'post' },
)

onBeforeUnmount(() => {
  cleanup?.()
  cleanup = null
  host.value?.replaceChildren()
})
</script>

<template>
  <section class="plugin-page">
    <el-alert v-if="errorMessage" :title="errorMessage" type="warning" :closable="false" />
    <div ref="host" class="plugin-host" />
  </section>
</template>
