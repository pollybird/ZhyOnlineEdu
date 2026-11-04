import { apiDelete, apiGet, apiPost } from './request'
import type { PluginEnabledListOut, PluginListOut, PluginOut, PluginUninstallOut } from './types'

/**
 * 插件接口（M3.5 / M3.6）。
 *
 * - `pluginApi.enabled()`：登录即可读，只返回**已启用**插件的菜单与前端 ESM 入口，
 *   前端据此渲染插件菜单并动态 `import()`（禁用 / 失败 / 未安装的插件不会出现）；
 * - `pluginAdminApi.*`：管理端接口，后端统一要求 `system:manage`。
 */
export const pluginApi = {
  enabled: (): Promise<PluginEnabledListOut> => apiGet('/plugins'),
}

export const pluginAdminApi = {
  list: (): Promise<PluginListOut> => apiGet('/system/plugins'),
  install: (code: string): Promise<PluginOut> => apiPost(`/system/plugins/${code}/install`),
  enable: (code: string): Promise<PluginOut> => apiPost(`/system/plugins/${code}/enable`),
  disable: (code: string): Promise<PluginOut> => apiPost(`/system/plugins/${code}/disable`),
  uninstall: (code: string): Promise<PluginUninstallOut> =>
    apiDelete(`/system/plugins/${code}`),
}
