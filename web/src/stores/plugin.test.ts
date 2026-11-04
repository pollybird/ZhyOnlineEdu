import { describe, expect, it } from 'vitest'
import type { ApiEnvelope } from '../api/types'
import {
  capabilityLabel,
  envelopeOk,
  pluginAssetUrl,
  pluginPagePath,
  pluginStateLabel,
  pluginStateTag,
  visibleMenus,
  type PluginMenuItem,
} from './plugin'

function envelope(overrides: Partial<ApiEnvelope<unknown>> = {}): ApiEnvelope<unknown> {
  return { code: 'OK', data: null, request_id: 'req', ...overrides }
}

function menu(overrides: Partial<PluginMenuItem> = {}): PluginMenuItem {
  return {
    code: 'demo',
    title: '插件面板',
    path: '/plugin/demo',
    permission: null,
    ...overrides,
  }
}

describe('插件前端入口（M3.5）', () => {
  it('entry_web 映射为免鉴权资源地址', () => {
    expect(pluginAssetUrl('demo', 'web/index.js')).toBe('/api/v1/plugin/demo/assets/index.js')
    expect(pluginAssetUrl('demo', 'web\\nested\\app.mjs')).toBe(
      '/api/v1/plugin/demo/assets/nested/app.mjs',
    )
  })

  it('未声明或不在 web/ 下的入口不给地址（避免拼出不存在的路径）', () => {
    expect(pluginAssetUrl('demo', null)).toBeNull()
    expect(pluginAssetUrl('demo', undefined)).toBeNull()
    expect(pluginAssetUrl('demo', 'index.js')).toBeNull()
  })

  it('插件页面路由兼容带/不带子路径', () => {
    expect(pluginPagePath('demo')).toBe('/plugin/demo')
    expect(pluginPagePath('demo', 'report')).toBe('/plugin/demo/report')
    expect(pluginPagePath('demo', '/report')).toBe('/plugin/demo/report')
  })
})

describe('插件菜单显隐', () => {
  it('未声明 permission 的菜单登录后即可见', () => {
    expect(visibleMenus([menu()], () => false)).toEqual([menu()])
  })

  it('声明 permission 的菜单按权限过滤', () => {
    const items = [menu(), menu({ title: '证书', path: '/plugin/demo/cert', permission: 'course:edit' })]
    const visible = visibleMenus(items, (code) => code === 'course:edit')
    expect(visible.map((item) => item.title)).toEqual(['插件面板', '证书'])
    expect(visibleMenus(items, () => false).map((item) => item.title)).toEqual(['插件面板'])
  })

  it('空清单不报错', () => {
    expect(visibleMenus([], () => true)).toEqual([])
  })
})

describe('插件分发请求信封判定', () => {
  it('HTTP 成功且 code=OK 才算成功', () => {
    expect(envelopeOk(envelope(), 200)).toBe(true)
    expect(envelopeOk(envelope({ code: 0 }), 200)).toBe(true)
  })

  it('HTTP 失败 / 无响应体 / 业务错误码均视为失败', () => {
    expect(envelopeOk(envelope(), 500)).toBe(false)
    expect(envelopeOk(null, 200)).toBe(false)
    expect(envelopeOk(envelope({ code: 'PLUGIN_CAP_DENIED' }), 403)).toBe(false)
  })
})

describe('插件状态与能力文案', () => {
  it('三种状态映射为中文并给出标签色', () => {
    expect(pluginStateLabel('enabled')).toBe('已启用')
    expect(pluginStateLabel('disabled')).toBe('未启用')
    expect(pluginStateLabel('failed')).toBe('加载失败')
    expect(pluginStateLabel('future')).toBe('未知状态')
    expect(pluginStateTag('enabled')).toBe('success')
    expect(pluginStateTag('failed')).toBe('warning')
    expect(pluginStateTag('disabled')).toBe('info')
  })

  it('白名单能力有中文文案，未知能力保留原码', () => {
    expect(capabilityLabel('progress:report')).toBe('上报学习进度')
    expect(capabilityLabel('media:ticket:issue')).toBe('签发降权播放凭证')
    expect(capabilityLabel('future:cap')).toBe('future:cap')
  })
})
