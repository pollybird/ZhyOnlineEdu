/**
 * TicketedHls —— hls.js 封装（§10.3.4.2 / §14.5 · F3/F4 前端唯一入口）。
 *
 * 红线：业务禁止直接 new Hls()。
 * - 自定义 fLoader 拦截 playlist 与 fragment 请求；
 * - 403/401 → 调 ticket_refresh_url 换票并重放当前 fragment，最多 2 轮；
 * - iOS / WKWebView 原生 HLS：取 key 走 query-string 票据（不经过 XHR），
 *   换票 = video.pause() → video.src = newPlaylist → play()（§23.3 协议要求）；
 * - hls.js 锁小版本 1.5.x，升级须重跑 MEDIA-001~008（附录 F）。
 *
 * M2.5 交付；此处为 M1.7 骨架占位。
 */
export class TicketedHls {
  static isNativeHls(video: HTMLVideoElement): boolean {
    return video.canPlayType('application/vnd.apple.mpegurl') !== ''
  }
}
