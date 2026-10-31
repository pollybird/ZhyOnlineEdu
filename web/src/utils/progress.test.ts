import { describe, expect, it } from 'vitest'
import { computePlayedDelta, progressBucket } from './progress'

describe('进度桶（与后端 position//30 同口径）', () => {
  it('按 30 秒向上取整桶', () => {
    expect(progressBucket(0)).toBe(0)
    expect(progressBucket(29)).toBe(0)
    expect(progressBucket(30)).toBe(1)
    expect(progressBucket(91)).toBe(3)
  })

  it('非法/负数按 0', () => {
    expect(progressBucket(-5)).toBe(0)
    expect(progressBucket(Number.NaN)).toBe(0)
  })

  it('桶宽可配置', () => {
    expect(progressBucket(90, 60)).toBe(1)
    expect(progressBucket(120, 60)).toBe(2)
  })
})

describe('播放增量（防止 seek 被计入学习时长）', () => {
  it('正常前进按真实增量', () => {
    expect(computePlayedDelta(10, 14, 5)).toBe(4)
  })

  it('向后拖动 / 暂停不累计', () => {
    expect(computePlayedDelta(30, 10, 5)).toBe(0)
    expect(computePlayedDelta(10, 10, 5)).toBe(0)
  })

  it('向前跳转按 maxStep 截断', () => {
    expect(computePlayedDelta(10, 90, 5)).toBe(5)
  })

  it('非法值不累计', () => {
    expect(computePlayedDelta(Number.NaN, 10, 5)).toBe(0)
    expect(computePlayedDelta(10, Number.POSITIVE_INFINITY, 5)).toBe(0)
  })
})
