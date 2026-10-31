/**
 * TicketedHls —— 带票据的 HLS 播放器（§10.3.4.2 / §14.5 / §23.3 · F3/F4）。
 *
 * 红线：业务禁止直接 `new Hls()`；所有播放都必须经过本组件。
 *
 * - **hls.js 路径**：`playlist_url` 已带 `ks/n/exp/sig`，分片与 key 同样签名；
 *   网络类致命错误（含 401/403）→ 重新取票并重放当前片段，最多 2 轮；
 * - **原生 HLS（iOS / 微信 WKWebView）**：key 请求不经过 XHR，只能依赖 URL 上的
 *   query 票据；因此原生路径只做「预换票 + 失效时 `pause → 换 src → play`」；
 * - 同一次票据在两条路径下收到的 URL 字节相同，便于服务端统一校验；
 * - 换票保留播放位置，并采用本地水位（不回跳）。
 */

import Hls from 'hls.js'

import type { TicketOut } from '../api/types'

export type PlayerStatus = 'idle' | 'loading' | 'ready' | 'refreshing' | 'error'

export interface TicketedHlsOptions {
  lessonId: number
  fetchTicket: () => Promise<TicketOut>
  onStatus?: (status: PlayerStatus) => void
  onTicket?: (ticket: TicketOut) => void
  onFatal?: (error: unknown) => void
  /** 换票前预留秒数（默认 20，与 `x-zhy-ticket-ttl.refresh_before_sec` 对齐）。 */
  refreshBeforeSec?: number
}

const MAX_REFRESH = 2

/** 原生 HLS 判定（纯函数，便于单测：注入 canPlayType 即可）。 */
export function supportsNativeHls(canPlayType: (type: string) => string): boolean {
  return canPlayType('application/vnd.apple.mpegurl') !== ''
}

export class TicketedHls {
  private readonly video: HTMLVideoElement
  private readonly options: TicketedHlsOptions
  private ticket: TicketOut | null = null
  private instance: Hls | null = null
  private refreshes = 0
  private refreshAtPosition = 0
  private refreshTimer: ReturnType<typeof setTimeout> | null = null
  private destroyed = false
  private native = false
  private resumeAt = 0
  private wantPlay = false
  /** 用户手动选择的清晰度（`null` = 自动按网络切换）。 */
  private preferredProfile: string | null = null

  constructor(video: HTMLVideoElement, options: TicketedHlsOptions) {
    this.video = video
    this.options = options
    this.video.playsInline = true
    this.video.addEventListener('timeupdate', this.onProgress)
  }

  static isNativeHls(video: HTMLVideoElement): boolean {
    return supportsNativeHls((type) => video.canPlayType(type))
  }

  get current(): TicketOut | null {
    return this.ticket
  }

  /** 原生 HLS 无法在 JS 层切档，只有 hls.js 路径支持手动选择清晰度。 */
  get supportsProfileSwitch(): boolean {
    return !this.native && this.instance !== null
  }

  /**
   * 手动选择清晰度（`360` / `720` / `1080`）；传 `null` 回到自动模式。
   * 切档立即生效并保留播放位置；换票重载后会再次套用同一选择。
   */
  setProfile(profile: string | null): void {
    this.preferredProfile = profile
    this.applyPreferred()
  }

  /** 首次取票并装载；失败原样抛出（由页面映射为五态）。 */
  async load(): Promise<TicketOut> {
    this.setStatus('loading')
    const ticket = await this.options.fetchTicket()
    if (this.destroyed) return ticket
    this.apply(ticket, 0, false)
    return ticket
  }

  /**
   * 手动/错误换票（保留位置与播放意图）。
   *
   * `scheduled=true` 是正常的票据续签（key_ttl 到期前主动换票），不计入
   * `MAX_REFRESH`；否则长视频会在第三次续签时被误判为「无法恢复」。
   */
  async refresh(options: { scheduled?: boolean } = {}): Promise<void> {
    if (this.destroyed) return
    const scheduled = options.scheduled === true
    if (!scheduled && this.refreshes >= MAX_REFRESH) {
      this.fail(new Error('ticket_refresh_exhausted'))
      return
    }
    if (!scheduled) {
      this.refreshes += 1
      this.refreshAtPosition = this.video.currentTime
    }
    this.setStatus('refreshing')
    try {
      const ticket = await this.options.fetchTicket()
      if (this.destroyed) return
      this.apply(ticket, this.video.currentTime, this.wantPlay || !this.video.paused)
      this.wantPlay = true
    } catch (error) {
      this.fail(error)
    }
  }

  destroy(): void {
    this.destroyed = true
    if (this.refreshTimer !== null) {
      clearTimeout(this.refreshTimer)
      this.refreshTimer = null
    }
    this.video.removeEventListener('error', this.onNativeError)
    this.video.removeEventListener('loadedmetadata', this.onLoadedMetadata)
    this.video.removeEventListener('timeupdate', this.onProgress)
    this.instance?.destroy()
    this.instance = null
    this.ticket = null
  }

  private apply(ticket: TicketOut, resume: number, play: boolean): void {
    this.ticket = ticket
    this.resumeAt = Math.max(resume, ticket.resume_second, 0)
    this.wantPlay = play
    this.options.onTicket?.(ticket)
    this.native = TicketedHls.isNativeHls(this.video) || !Hls.isSupported()
    if (this.native) {
      this.applyNative(ticket.playlist_url)
    } else {
      this.applyHlsJs(ticket.playlist_url)
    }
    this.scheduleRefresh(ticket.key_ttl)
    this.setStatus('ready')
  }

  private applyNative(url: string): void {
    this.instance?.destroy()
    this.instance = null
    this.video.removeEventListener('error', this.onNativeError)
    this.video.addEventListener('error', this.onNativeError)
    this.video.addEventListener('loadedmetadata', this.onLoadedMetadata, { once: true })
    this.video.src = url
    this.video.load()
    if (this.wantPlay) {
      void this.video.play().catch(() => {
        /* 自动播放被拦截：由用户手势触发，不视为致命错误 */
      })
    }
  }

  private applyHlsJs(url: string): void {
    this.video.removeEventListener('error', this.onNativeError)
    if (this.instance !== null) {
      this.instance.loadSource(url)
      if (this.wantPlay) void this.video.play().catch(() => undefined)
      return
    }
    const instance = new Hls({ enableWorker: true, lowLatencyMode: false })
    instance.attachMedia(this.video)
    instance.loadSource(url)
    instance.on(Hls.Events.ERROR, (_event, data: unknown) => {
      if (typeof data !== 'object' || data === null) return
      const detail = data as { fatal?: boolean; type?: string; response?: { code?: number } }
      if (detail.fatal !== true) return
      if (detail.type === 'networkError') {
        void this.refresh()
        return
      }
      this.fail(new Error(String(detail.type ?? 'playback_error')))
    })
    instance.on(Hls.Events.MANIFEST_PARSED, () => {
      if (this.resumeAt > 0) this.video.currentTime = this.resumeAt
      this.applyPreferred()
      if (this.wantPlay) void this.video.play().catch(() => undefined)
    })
    this.instance = instance
  }

  /**
   * 把用户选择的清晰度映射到 hls.js 档位索引：
   * - `preferredProfile === null` → `-1`（自动，由 ABR 决定）；
   - 找不到对应档位时同样回落到自动，避免停在不可用档位。
   */
  private applyPreferred(): void {
    if (this.instance === null) return
    const level = this.preferredProfile === null ? -1 : this.instance.levels.findIndex(
      (item) => item.height === Number(this.preferredProfile),
    )
    this.instance.currentLevel = level >= 0 ? level : -1
  }

  private onLoadedMetadata = (): void => {
    if (this.resumeAt > 0 && Number.isFinite(this.video.duration)) {
      this.video.currentTime = Math.min(this.resumeAt, Math.max(0, this.video.duration - 1))
    }
    if (this.wantPlay) {
      void this.video.play().catch(() => undefined)
    }
  }

  private onNativeError = (): void => {
    if (this.destroyed) return
    void this.refresh()
  }

  /**
   * 换票预算复位：错误换票后只要能继续播放 5s 以上，就认为「已恢复」，
   * 重新给足 `MAX_REFRESH` 次数；否则整场播放的总预算只有 2 次。
   */
  private onProgress = (): void => {
    if (this.destroyed || this.refreshes === 0) return
    if (this.video.readyState >= 3 && this.video.currentTime > this.refreshAtPosition + 5) {
      this.refreshes = 0
    }
  }

  private scheduleRefresh(keyTtl: number): void {
    if (this.refreshTimer !== null) clearTimeout(this.refreshTimer)
    const lead = this.options.refreshBeforeSec ?? 20
    const delay = Math.max(30, Math.round((keyTtl - lead) * 1000))
    this.refreshTimer = setTimeout(() => {
      if (!this.destroyed && (this.video.paused || this.video.readyState >= 2)) {
        void this.refresh({ scheduled: true })
      }
    }, delay)
  }

  private fail(error: unknown): void {
    this.setStatus('error')
    this.options.onFatal?.(error)
  }

  private setStatus(status: PlayerStatus): void {
    this.options.onStatus?.(status)
  }
}
