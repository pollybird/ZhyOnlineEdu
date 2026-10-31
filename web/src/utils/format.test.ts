import { describe, expect, it } from 'vitest'
import {
  accessLabel,
  describeError,
  errorCodeOf,
  formatBytes,
  formatDuration,
  formatPercent,
  formatRelative,
  freshnessLabel,
  mergeResume,
} from './format'

describe('错误码 → 五态映射（M2.14）', () => {
  it('会员课无权限时给出开通会员引导', () => {
    const view = describeError({ code: 'ACCESS_LEVEL_REQUIRED' })
    expect(view.state).toBe('forbidden')
    expect(view.action).toBe('member')
  })

  it('班级课无权限时引导返回', () => {
    const view = describeError({ code: 'ACCESS_CLASS_REQUIRED' })
    expect(view.state).toBe('forbidden')
    expect(view.action).toBe('back')
  })

  it('课程不存在映射为空态', () => {
    expect(describeError({ code: 'NOT_FOUND' }).state).toBe('empty')
  })

  it('播放凭证类错误码映射为播放态文案', () => {
    expect(describeError({ code: 40201 }).title).toBe('播放凭证已过期')
    expect(describeError({ code: '40205' }).title).toBe('播放无法恢复')
  })

  it('网络错误可重试', () => {
    const view = describeError({})
    expect(view.code).toBe('NETWORK_ERROR')
    expect(view.action).toBe('retry')
  })

  it('errorCodeOf 兼容字符串 / 数字 / 未知对象', () => {
    expect(errorCodeOf({ code: 40202 })).toBe('40202')
    expect(errorCodeOf(null)).toBe('NETWORK_ERROR')
    expect(errorCodeOf(new Error('boom'))).toBe('NETWORK_ERROR')
  })
})

describe('展示格式化', () => {
  it('时长按 mm:ss / h:mm:ss', () => {
    expect(formatDuration(0)).toBe('0:00')
    expect(formatDuration(65)).toBe('1:05')
    expect(formatDuration(3661)).toBe('1:01:01')
    expect(formatDuration(Number.NaN)).toBe('0:00')
  })

  it('百分比保留 1 位并去掉无意义 .0', () => {
    expect(formatPercent(0)).toBe('0%')
    expect(formatPercent(33.33)).toBe('33.3%')
    expect(formatPercent(120)).toBe('100%')
    expect(formatPercent(Number.NaN)).toBe('0%')
  })

  it('相对时间按窗口分段', () => {
    const now = new Date('2026-10-30T12:00:00Z')
    expect(formatRelative('2026-10-30T11:59:40Z', now)).toBe('刚刚')
    expect(formatRelative('2026-10-30T11:30:00Z', now)).toBe('30 分钟前')
    expect(formatRelative('2026-10-30T10:00:00Z', now)).toBe('2 小时前')
    expect(formatRelative('2026-10-28T12:00:00Z', now)).toBe('2 天前')
    expect(formatRelative(null, now)).toBe('—')
    expect(formatRelative('not-a-date', now)).toBe('—')
  })

  it('投影滞后文案', () => {
    expect(freshnessLabel(0)).toBe('已是最新')
    expect(freshnessLabel(30)).toBe('更新于 30 秒前')
    expect(freshnessLabel(120)).toBe('更新于 2 分钟前')
  })

  it('文件大小与续播水位', () => {
    expect(formatBytes(0)).toBe('0 B')
    expect(formatBytes(2048)).toBe('2 KB')
    expect(mergeResume(10, 42)).toBe(42)
    expect(mergeResume(42, 10)).toBe(42)
    expect(mergeResume(Number.NaN, 10)).toBe(10)
  })

  it('授权类型标签', () => {
    expect(accessLabel('public')).toBe('公开课')
    expect(accessLabel('member')).toBe('会员课')
    expect(accessLabel('class')).toBe('班级课')
    expect(accessLabel('other')).toBe('other')
  })
})
