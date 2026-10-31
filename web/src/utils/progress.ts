/**
 * 进度上报纯逻辑（M2.5 / M2.14）：30s 桶与播放增量计算，便于 vitest 覆盖。
 */

/** 与后端 `position_bucket = position // 30` 同口径（去重键）。 */
export function progressBucket(position: number, bucketSec = 30): number {
  const safe = Number.isFinite(position) ? Math.max(0, Math.floor(position)) : 0
  return Math.floor(safe / Math.max(1, bucketSec))
}

/**
 * 计算一次心跳间真实播放的秒数。
 *
 * - 向后拖拽 / 原地暂停 → 0；
 * - 向前跳转（含 seek）会被 `maxStep` 截断，避免把跳过区间算成学习时长；
 * - 倍速按 1x 口径由后端折算，这里只累计屏幕播放秒数。
 */
export function computePlayedDelta(previous: number, current: number, maxStep = 5): number {
  if (!Number.isFinite(previous) || !Number.isFinite(current)) return 0
  const delta = current - previous
  if (delta <= 0) return 0
  return Math.min(Math.floor(delta), Math.max(1, Math.floor(maxStep)))
}
