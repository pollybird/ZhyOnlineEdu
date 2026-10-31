import { describe, expect, it } from 'vitest'

import type { TicketOut } from '../api/types'
import { TicketedHls, supportsNativeHls } from './TicketedHls'

describe('原生 HLS 判定（iOS / 微信 WKWebView · §23.3）', () => {
  it('canPlayType 返回非空即走原生路径', () => {
    const canPlay = (type: string): string =>
      type === 'application/vnd.apple.mpegurl' ? 'probably' : ''
    expect(supportsNativeHls(canPlay)).toBe(true)
  })

  it('不支持时回落 hls.js 路径', () => {
    expect(supportsNativeHls(() => '')).toBe(false)
  })

  it('空字符串视为不支持（Safari 之外的浏览器）', () => {
    const canPlay = (): string => ''
    expect(supportsNativeHls(canPlay)).toBe(false)
  })
})

// ── 换票预算（长视频定时续签不应被当成「无法恢复」） ─────────────────────

type Listener = () => void

function fakeVideo(): { video: HTMLVideoElement; emit: (type: string) => void } {
  const listeners = new Map<string, Set<Listener>>()
  const video = {
    playsInline: false,
    paused: false,
    readyState: 4,
    currentTime: 0,
    duration: 100,
    src: '',
    addEventListener: (type: string, callback: Listener) => {
      const set = listeners.get(type) ?? new Set<Listener>()
      set.add(callback)
      listeners.set(type, set)
    },
    removeEventListener: (type: string, callback: Listener) => {
      listeners.get(type)?.delete(callback)
    },
    canPlayType: () => '',
    load: () => undefined,
    play: () => Promise.resolve(),
  }
  return {
    video: video as unknown as HTMLVideoElement,
    emit: (type: string) => {
      for (const callback of listeners.get(type) ?? []) callback()
    },
  }
}

function ticket(index: number): TicketOut {
  return {
    lesson_id: 1,
    course_id: 1,
    kid: 'a'.repeat(32),
    asset_status: 'encoded',
    preview: false,
    profiles: ['360'],
    playlist_url: `/api/v1/media/playlist/${index}`,
    playlist_ttl: 300,
    key_ttl: 120,
    ticket_expires_at: '2026-10-30T00:00:00+00:00',
    resume_second: 0,
    watermark: { text: 'u', opacity: 0.12, position: 'br' },
    metric_version: 'v1',
  }
}

function build(): {
  player: TicketedHls
  video: HTMLVideoElement
  emit: (type: string) => void
  fatal: unknown[]
} {
  const { video, emit } = fakeVideo()
  let issued = 0
  const fatal: unknown[] = []
  const player = new TicketedHls(video, {
    lessonId: 1,
    fetchTicket: async () => {
      issued += 1
      return ticket(issued)
    },
    onFatal: (error) => fatal.push(error),
  })
  return { player, video, emit, fatal }
}

describe('换票预算（§10.3.4 · F4）', () => {
  it('定时续签不计入错误换票预算', async () => {
    const { player, fatal } = build()
    await player.load()
    for (let index = 0; index < 5; index += 1) {
      await player.refresh({ scheduled: true })
    }
    expect(fatal).toHaveLength(0)
    player.destroy()
  })

  it('错误换票超过 MAX_REFRESH 后报致命错误', async () => {
    const { player, fatal } = build()
    await player.load()
    await player.refresh()
    await player.refresh()
    expect(fatal).toHaveLength(0)
    await player.refresh()
    expect(fatal).toHaveLength(1)
    expect(String(fatal[0])).toContain('ticket_refresh_exhausted')
    player.destroy()
  })

  it('恢复播放超过 5 秒后重置错误换票预算', async () => {
    const { player, video, emit, fatal } = build()
    await player.load()
    await player.refresh()
    await player.refresh()
    video.currentTime = 6
    emit('timeupdate')
    await player.refresh()
    expect(fatal).toHaveLength(0)
    player.destroy()
  })
})

// ── 清晰度手动切档（播放页「清晰度」控件） ─────────────────────────────

describe('清晰度切档', () => {
  it('原生 HLS 路径不支持手动切档（页面隐藏控件）', async () => {
    const { player } = build()
    await player.load()
    expect(player.supportsProfileSwitch).toBe(false)
    player.destroy()
  })

  it('按档位高度映射到 hls.js 索引，找不到时回落自动', async () => {
    const { player } = build()
    await player.load()
    const stub = {
      levels: [{ height: 360 }, { height: 720 }, { height: 1080 }],
      currentLevel: -1,
      destroy: () => undefined,
    }
    ;(player as unknown as { instance: typeof stub }).instance = stub

    player.setProfile('720')
    expect(stub.currentLevel).toBe(1)

    player.setProfile('1080')
    expect(stub.currentLevel).toBe(2)

    player.setProfile('540')
    expect(stub.currentLevel).toBe(-1)

    player.setProfile(null)
    expect(stub.currentLevel).toBe(-1)
    player.destroy()
  })
})
