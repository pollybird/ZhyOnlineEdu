import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import { pluginApi } from '../api/plugins'
import { getAccessToken } from '../api/request'
import type { ApiEnvelope, PluginEnabledOut, PluginMenuOut } from '../api/types'

/**
 * 插件前端接入（M3.5）。
 *
 * 契约（与后端 `manifest.json` 的 `entry_web` 对应）：
 * - 插件在 `<plugin_dir>/web/` 下提供 ESM 入口，`export default` 一个模块对象：
 *   ```js
 *   export default {
 *     // 可选的菜单补充（后端 manifest.menu 已下发的菜单无需重复声明）
 *     setup(ctx) {},
 *     // key = manifest.routes 里的子路径（'' / 'index' 视作默认页）
 *     pages: {
 *       index(el, ctx) {
 *         el.innerHTML = '<h2>示例插件</h2>'
 *         // ctx.request('hello') 访问 /api/v1/plugin/<code>/hello（自动带登录态）
 *         return () => { el.innerHTML = '' }
 *       },
 *     },
 *   }
 *   ```
 * - 插件页面通过 `/plugin/<code>/<subpath>` 路由承载，销毁时会调用返回的清理函数；
 * - 加载失败只告警，不阻塞内核与其它插件（禁用后菜单与资源同时消失）。
 */
export interface PluginPageContext {
  code: string
  request<T>(path: string, init?: RequestInit): Promise<T>
}

export type PluginPageMount = (el: HTMLElement, ctx: PluginPageContext) => void | (() => void)

export interface PluginWebModule {
  pages?: Record<string, PluginPageMount>
}

export interface PluginMenuItem {
  code: string
  title: string
  path: string
  permission: string | null
}

/** `web/index.js` → `/api/v1/plugin/<code>/assets/index.js`；非法入口返回 null。 */
export function pluginAssetUrl(code: string, entryWeb: string | null | undefined): string | null {
  if (!entryWeb) {
    return null
  }
  const relative = entryWeb.replace(/\\/g, '/')
  if (!relative.startsWith('web/')) {
    return null
  }
  return `/api/v1/plugin/${code}/assets/${relative.slice('web/'.length)}`
}

/** 插件页面路由：`/plugin/<code>` 或 `/plugin/<code>/<subpath>`。 */
export function pluginPagePath(code: string, subpath?: string | null): string {
  const suffix = subpath ? `/${subpath.replace(/^\/+/, '')}` : ''
  return `/plugin/${code}${suffix}`
}

/** 菜单显隐：未声明 `permission` 表示登录即可见。 */
export function visibleMenus(
  menus: readonly PluginMenuItem[],
  can: (code: string) => boolean,
): PluginMenuItem[] {
  return menus.filter((item) => item.permission === null || can(item.permission))
}

/** 统一信封判定：HTTP 成功且 `code=OK` 才算成功（进度 202 不在此列）。 */
export function envelopeOk(body: ApiEnvelope<unknown> | null, http: number): boolean {
  if (http >= 400 || body === null) {
    return false
  }
  return body.code === 'OK' || body.code === 0
}

export function pluginStateLabel(state: string): string {
  switch (state) {
    case 'enabled':
      return '已启用'
    case 'failed':
      return '加载失败'
    case 'disabled':
      return '未启用'
    default:
      return '未知状态'
  }
}

export function pluginStateTag(state: string): 'success' | 'warning' | 'info' {
  switch (state) {
    case 'enabled':
      return 'success'
    case 'failed':
      return 'warning'
    default:
      return 'info'
  }
}

const CAPABILITY_LABELS: Record<string, string> = {
  'progress:read': '读取学习进度',
  'progress:report': '上报学习进度',
  'media:ticket:issue': '签发降权播放凭证',
  'access:evaluate': '只读访问判定',
  'config:read': '读取配置',
  'log:read': '读取日志',
}

/** 能力码文案；未知能力按原码展示，避免吞掉后端新增能力。 */
export function capabilityLabel(capability: string): string {
  return CAPABILITY_LABELS[capability] ?? capability
}

export const usePluginStore = defineStore('plugin', () => {
  const items = ref<PluginEnabledOut[]>([])
  const modules = ref<Record<string, PluginWebModule>>({})
  const loaded = ref(false)
  const loading = ref(false)
  const errorMessage = ref('')
  let pending: Promise<void> | null = null

  const menus = computed<PluginMenuItem[]>(() =>
    items.value.flatMap((item) =>
      item.menu.map((menu: PluginMenuOut) => ({
        code: item.code,
        title: menu.title,
        path: menu.path ?? pluginPagePath(item.code),
        permission: menu.permission ?? null,
      })),
    ),
  )

  async function loadModules(): Promise<void> {
    const next: Record<string, PluginWebModule> = {}
    for (const item of items.value) {
      const url = item.entry_web_url ?? pluginAssetUrl(item.code, item.entry_web)
      if (url === null) {
        continue
      }
      try {
        const namespace = (await import(/* @vite-ignore */ url)) as PluginWebModule & {
          default?: PluginWebModule
        }
        next[item.code] = namespace.default ?? namespace
      } catch (error) {
        // 单个插件前端加载失败不影响其它插件与内核
        console.warn(`[plugin] ${item.code} 前端入口加载失败`, error)
      }
    }
    modules.value = next
  }

  async function load(): Promise<void> {
    loading.value = true
    errorMessage.value = ''
    try {
      const payload = await pluginApi.enabled()
      items.value = payload.items
      loaded.value = true
      await loadModules()
    } catch (error) {
      errorMessage.value = error instanceof Error ? error.message : '插件清单加载失败'
    } finally {
      loading.value = false
    }
  }

  /** 首次进入时加载（单飞）：登录前后都可能调用，不会重复打接口。 */
  function ensureLoaded(): Promise<void> {
    if (loaded.value) {
      return Promise.resolve()
    }
    pending ??= load().finally(() => {
      pending = null
    })
    return pending
  }

  function page(code: string, subpath: string): PluginPageMount | null {
    const pages = modules.value[code]?.pages
    if (pages === undefined) {
      return null
    }
    return pages[subpath] ?? pages[subpath === '' ? 'index' : ''] ?? null
  }

  /** 插件页面上下文：`request()` 走统一分发入口并解包信封。 */
  function context(code: string): PluginPageContext {
    return {
      code,
      async request<T>(path: string, init?: RequestInit): Promise<T> {
        const response = await fetch(
          `/api/v1/plugin/${code}/${path.replace(/^\/+/, '')}`,
          {
            ...init,
            credentials: 'include',
            headers: {
              'Content-Type': 'application/json',
              Authorization: `Bearer ${getAccessToken()}`,
              ...(init?.headers ?? {}),
            },
          },
        )
        const body = (await response.json()) as ApiEnvelope<T>
        if (!envelopeOk(body, response.status)) {
          throw new Error(body.message ?? `请求失败（HTTP ${response.status}）`)
        }
        return body.data
      },
    }
  }

  function clear(): void {
    items.value = []
    modules.value = {}
    loaded.value = false
    errorMessage.value = ''
  }

  return {
    items,
    modules,
    loaded,
    loading,
    errorMessage,
    menus,
    load,
    ensureLoaded,
    page,
    context,
    clear,
  }
})
