import { apiGet, apiPost, getAccessToken } from './request'
import type {
  CourseProgressDetailOut,
  CourseProgressListOut,
  LearnCourseDetailOut,
  LearnCourseListOut,
  MySummaryOut,
  ProgressGateOut,
  ProgressOut,
  TicketOut,
} from './types'

export interface ProgressPayload {
  lesson_id: number
  position_sec: number
  played_seconds: number
  speed: number
  client_ts_ms: number
}

/** 学员端学习接口（M2.14 页面唯一入口）。 */
export const learnApi = {
  courses(params: {
    subject_id?: number
    keyword?: string
    page?: number
    page_size?: number
  }): Promise<LearnCourseListOut> {
    return apiGet<LearnCourseListOut>('/learn/courses', { params })
  },

  course(courseId: number): Promise<LearnCourseDetailOut> {
    return apiGet<LearnCourseDetailOut>(`/learn/courses/${courseId}`)
  },

  summary(): Promise<MySummaryOut> {
    return apiGet<MySummaryOut>('/my/summary')
  },

  myProgress(params: {
    status?: string
    keyword?: string
    page?: number
    page_size?: number
  }): Promise<CourseProgressListOut> {
    return apiGet<CourseProgressListOut>('/my/progress', { params })
  },

  myProgressDetail(courseId: number): Promise<CourseProgressDetailOut> {
    return apiGet<CourseProgressDetailOut>(`/my/progress/${courseId}`)
  },

  gate(): Promise<ProgressGateOut> {
    return apiGet<ProgressGateOut>('/my/progress/gate')
  },

  /** 取播放票据：未授权返回 403 ACCESS_*，未转码返回 409 40202。 */
  ticket(lessonId: number): Promise<TicketOut> {
    return apiPost<TicketOut>('/media/tickets', { lesson_id: lessonId })
  },

  reportProgress(payload: ProgressPayload): Promise<ProgressOut> {
    return apiPost<ProgressOut>('/learn/progress', payload)
  },
}

/**
 * 页面隐藏时的兜底上报：`sendBeacon` 无法携带 Bearer，改用 `fetch(keepalive)`。
 * 失败不抛出（只在控制台留痕），由服务端 `GREATEST(last_position)` 安全网补齐。
 */
export async function reportProgressKeepalive(payload: ProgressPayload): Promise<void> {
  const token = getAccessToken()
  const headers: Record<string, string> = { 'Content-Type': 'application/json' }
  if (token !== '') headers.Authorization = `Bearer ${token}`
  const response = await fetch('/api/v1/learn/progress', {
    method: 'POST',
    keepalive: true,
    headers,
    body: JSON.stringify(payload),
  })
  if (!response.ok) {
    throw new Error(`progress keepalive failed: ${response.status}`)
  }
}
