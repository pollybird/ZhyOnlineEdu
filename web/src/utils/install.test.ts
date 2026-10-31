import { describe, expect, it } from 'vitest'
import { resolveInstallStep } from './install'

describe('resolveInstallStep', () => {
  it('令牌换取成功后（服务端仍报 start + 本地有会话）进入数据库步骤', () => {
    expect(resolveInstallStep('start', true)).toBe('db')
  })

  it('未换取会话时停留在令牌页', () => {
    expect(resolveInstallStep('start', false)).toBe('start')
  })

  it('服务端已推进的步骤原样透传', () => {
    expect(resolveInstallStep('db', true)).toBe('db')
    expect(resolveInstallStep('admin', true)).toBe('admin')
    expect(resolveInstallStep('mode', true)).toBe('mode')
    expect(resolveInstallStep('finish', true)).toBe('finish')
    expect(resolveInstallStep('done', false)).toBe('done')
  })
})
