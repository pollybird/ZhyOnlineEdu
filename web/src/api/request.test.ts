import { describe, expect, it } from 'vitest'
import { ApiError, isMediaCode, shouldRelogin, singleFlight } from './request'

describe('播放票据错误判定（402xx 不跳登录，§14.1）', () => {
  it('识别 40201~40206', () => {
    for (const code of [40201, 40202, 40203, 40204, 40205, 40206]) {
      expect(isMediaCode(code)).toBe(true)
    }
  })

  it('不把 AUTH_*/字符串码当播放码', () => {
    expect(isMediaCode('AUTH_TOKEN_EXPIRED')).toBe(false)
    expect(isMediaCode('40201')).toBe(false)
    expect(isMediaCode(401)).toBe(false)
    expect(isMediaCode(40207)).toBe(false)
    expect(isMediaCode(undefined)).toBe(false)
  })
})

describe('登录态失效判定（附录 A action=relogin）', () => {
  it('只有 relogin 码才允许刷新/回登录页', () => {
    for (const code of [
      'AUTH_TOKEN_EXPIRED',
      'AUTH_TOKEN_REVOKED',
      'AUTH_REFRESH_REPLAY',
      'ACCOUNT_EXPIRED',
      'ACCOUNT_DISABLED',
    ]) {
      expect(shouldRelogin(code)).toBe(true)
    }
  })

  it('业务错误与播放码不触发登录跳转', () => {
    expect(shouldRelogin('AUTH_INVALID_CREDENTIALS')).toBe(false)
    expect(shouldRelogin('INSTALL_TOKEN_INVALID')).toBe(false)
    expect(shouldRelogin(40201)).toBe(false)
    expect(shouldRelogin(null)).toBe(false)
  })
})

describe('singleFlight 单飞刷新', () => {
  it('并发调用共享同一个任务', async () => {
    let calls = 0
    const refresh = singleFlight(async () => {
      calls += 1
      return calls
    })

    const [first, second] = await Promise.all([refresh(), refresh()])
    expect(first).toBe(1)
    expect(second).toBe(1)
    expect(calls).toBe(1)

    expect(await refresh()).toBe(2)
    expect(calls).toBe(2)
  })

  it('失败后复位，可再次尝试', async () => {
    let attempt = 0
    const refresh = singleFlight(async () => {
      attempt += 1
      if (attempt === 1) {
        throw new Error('boom')
      }
      return attempt
    })

    await expect(refresh()).rejects.toThrow('boom')
    expect(await refresh()).toBe(2)
  })
})

describe('ApiError 信封信息', () => {
  it('保留 code/http/data/request_id', () => {
    const error = new ApiError('AUTH_TOKEN_EXPIRED', '登录已过期', 401, { reason: 'expired' }, 'rid-1')
    expect(error.name).toBe('ApiError')
    expect(error.code).toBe('AUTH_TOKEN_EXPIRED')
    expect(error.http).toBe(401)
    expect(error.data).toEqual({ reason: 'expired' })
    expect(error.requestId).toBe('rid-1')
    expect(error.message).toBe('登录已过期')
  })
})
