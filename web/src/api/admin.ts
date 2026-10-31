import SparkMD5 from 'spark-md5'

import { apiDelete, apiGet, apiPatch, apiPost } from './request'
import type {
  AssetListOut,
  AssetOut,
  ClassListOut,
  ClassMembersOut,
  CourseDetailOut,
  ImportBatchListOut,
  ImportBatchOut,
  MemberGrantOut,
  MemberLevelListOut,
  MemberLevelOut,
  PayNotifyOut,
  PayOrderOut,
  QuizAttemptOut,
  QuizQuestionListOut,
  QuizQuestionOut,
  RetranscodeOut,
  SubscriptionListOut,
  UploadCompleteOut,
  UploadInitOut,
  UploadStatusOut,
  UserListOut,
  UserOut,
} from './types'

/** 用户 / 班级 / 导入（M2.7）。 */
export const usersAdminApi = {
  list(params: {
    status?: string
    role?: string
    class_id?: number
    keyword?: string
    page?: number
    page_size?: number
  }): Promise<UserListOut> {
    return apiGet<UserListOut>('/users', { params })
  },

    create(body: {
    username: string
    password: string
    user_no?: string
    display_name?: string
    org_id?: number
    expire_at?: string
    roles?: string[]
  }): Promise<UserOut> {
    return apiPost<UserOut>('/users', body)
  },

  get(userId: number): Promise<UserOut> {
    return apiGet<UserOut>(`/users/${userId}`)
  },

  update(userId: number, body: Record<string, unknown>): Promise<UserOut> {
    return apiPatch<UserOut>(`/users/${userId}`, body)
  },

  disable(userId: number, version: number): Promise<UserOut> {
    return apiPost<UserOut>(`/users/${userId}/disable`, { version })
  },

  expire(userId: number, version: number, expireAt?: string): Promise<UserOut> {
    return apiPost<UserOut>(`/users/${userId}/expire`, { version, expire_at: expireAt })
  },

  imports(params: { status?: string; page?: number } = {}): Promise<ImportBatchListOut> {
    return apiGet<ImportBatchListOut>('/users/imports', { params })
  },

  importBatch(batchNo: string): Promise<ImportBatchOut> {
    return apiGet<ImportBatchOut>(`/users/imports/${batchNo}`)
  },

  importJson(
    rows: {
      username: string
      password?: string
      user_no?: string
      display_name?: string
      expire_at?: string
    }[],
    onConflict?: 'report_only' | 'overwrite',
  ): Promise<ImportBatchOut> {
    return apiPost<ImportBatchOut>('/users/import', {
      rows,
      on_conflict_action: onConflict,
    })
  },
}

/** 班级（M2.7）。 */
export const classAdminApi = {
  list(params: { keyword?: string; is_enabled?: string } = {}): Promise<ClassListOut> {
    return apiGet<ClassListOut>('/classes', { params })
  },

  create(body: {
    name: string
    teacher_id?: number
    is_enabled?: boolean
  }): Promise<{ id: number; name: string }> {
    return apiPost('/classes', body)
  },

  addMembers(classId: number, userIds: number[]): Promise<ClassMembersOut> {
    return apiPost<ClassMembersOut>(`/classes/${classId}/members`, { user_ids: userIds })
  },

  removeMember(classId: number, userId: number): Promise<{ removed: boolean }> {
    return apiDelete(`/classes/${classId}/members/${userId}`)
  },
}

/** 题库管理（M2.6）：管理端响应含标准答案，学员端永不包含。 */
export const quizAdminApi = {
  questions(params: {
    course_id?: number
    lesson_id?: number
    kind?: string
    is_enabled?: string
    page?: number
    page_size?: number
  }): Promise<QuizQuestionListOut> {
    return apiGet<QuizQuestionListOut>('/quiz/questions', { params })
  },

  create(body: Record<string, unknown>): Promise<QuizQuestionOut> {
    return apiPost<QuizQuestionOut>('/quiz/questions', body)
  },

  update(questionId: number, body: Record<string, unknown>): Promise<QuizQuestionOut> {
    return apiPatch<QuizQuestionOut>(`/quiz/questions/${questionId}`, body)
  },

  remove(questionId: number, version: number): Promise<{ deleted: boolean }> {
    return apiDelete(`/quiz/questions/${questionId}`, { version })
  },

  attempts(params: { lesson_id?: number; page?: number } = {}): Promise<{
    items: {
      id: number
      status: string
      score_earned: number
      score_total: number
      percent: number
      passed: boolean
      started_at: string | null
      submitted_at: string | null
    }[]
    total: number
  }> {
    return apiGet('/my/quiz/attempts', { params })
  },

  attempt(attemptId: number): Promise<QuizAttemptOut> {
    return apiGet<QuizAttemptOut>(`/quiz/attempts/${attemptId}`)
  },
}

/** 课程目录（管理端）：仅用于媒体/题库页选择课时。 */
export const courseAdminApi = {
  courses(params: { keyword?: string; page?: number; page_size?: number } = {}): Promise<{
    items: {
      id: number
      title: string
      status: string
      subject_name: string | null
    }[]
    total: number
  }> {
    return apiGet('/courses', { params })
  },

  detail(courseId: number): Promise<CourseDetailOut> {
    return apiGet<CourseDetailOut>(`/courses/${courseId}`)
  },
}

export interface UploadProgress {
  uploadedChunks: number
  totalChunks: number
  percent: number
}

function toHex(buffer: ArrayBuffer): string {
  return Array.from(new Uint8Array(buffer))
    .map((byte) => byte.toString(16).padStart(2, '0'))
    .join('')
}

async function sha256Hex(buffer: ArrayBuffer): Promise<string> {
  const digest = await crypto.subtle.digest('SHA-256', buffer)
  return toHex(digest)
}

function md5Hex(buffer: ArrayBuffer): string {
  const spark = new SparkMD5.ArrayBuffer()
  spark.append(buffer)
  return spark.end()
}

/** 媒体资产与分片上传（M2.3 / M2.4）。 */
export const mediaAdminApi = {
  assets(params: {
    course_id?: number
    lesson_id?: number
    page?: number
    page_size?: number
  } = {}): Promise<AssetListOut> {
    return apiGet<AssetListOut>('/media/assets', { params })
  },

  asset(lessonId: number): Promise<AssetOut> {
    return apiGet<AssetOut>(`/media/assets/${lessonId}`)
  },

  retranscode(lessonId: number, profiles?: string[]): Promise<RetranscodeOut> {
    return apiPost<RetranscodeOut>(`/media/assets/${lessonId}/retranscode`, { profiles })
  },

  remove(lessonId: number): Promise<{ deleted: boolean }> {
    return apiDelete(`/media/assets/${lessonId}`)
  },

  status(uploadId: string): Promise<UploadStatusOut> {
    return apiGet<UploadStatusOut>(`/media/uploads/${uploadId}`)
  },

  /** 整文件 sha256 + 分片 md5 校验；失败时保留 upload_id 供断点续传。 */
  async upload(
    lessonId: number,
    file: File,
    onProgress?: (progress: UploadProgress) => void,
  ): Promise<UploadCompleteOut> {
    const chunkSize = 8 * 1024 * 1024
    const totalChunks = Math.max(1, Math.ceil(file.size / chunkSize))
    const buffer = await file.arrayBuffer()
    const sha256 = await sha256Hex(buffer)
    const init = await apiPost<UploadInitOut>('/media/uploads', {
      lesson_id: lessonId,
      file_name: file.name,
      file_size: file.size,
      sha256,
      chunk_size: chunkSize,
    })
    for (let index = 0; index < totalChunks; index += 1) {
      const start = index * chunkSize
      const end = Math.min(file.size, start + chunkSize)
      const chunk = await file.slice(start, end).arrayBuffer()
      await apiPost(
        `/media/uploads/${init.upload_id}/chunks`,
        chunk,
        {
          params: { index, md5: md5Hex(chunk) },
          headers: { 'Content-Type': 'application/octet-stream' },
          timeout: 120_000,
        },
      )
      onProgress?.({
        uploadedChunks: index + 1,
        totalChunks,
        percent: Math.round(((index + 1) / totalChunks) * 100),
      })
    }
    return apiPost<UploadCompleteOut>(`/media/uploads/${init.upload_id}/complete`)
  },
}

/** 会员档位 / 订阅 / 赠送 / 查单（M2.12）。 */
export const memberAdminApi = {
  levels(params: { is_enabled?: string } = {}): Promise<MemberLevelListOut> {
    return apiGet<MemberLevelListOut>('/member/levels', { params })
  },

  createLevel(body: Record<string, unknown>): Promise<MemberLevelOut> {
    return apiPost<MemberLevelOut>('/member/levels', body)
  },

  updateLevel(levelId: number, body: Record<string, unknown>): Promise<MemberLevelOut> {
    return apiPatch<MemberLevelOut>(`/member/levels/${levelId}`, body)
  },

  subscriptions(params: {
    user_id?: number
    status?: string
    page?: number
  } = {}): Promise<SubscriptionListOut> {
    return apiGet<SubscriptionListOut>('/member/subscriptions', { params })
  },

  grant(body: { user_id: number; level_id: number; duration_days?: number }): Promise<MemberGrantOut> {
    return apiPost<MemberGrantOut>('/member/grant', body)
  },

  order(orderNo: string): Promise<PayOrderOut> {
    return apiGet<PayOrderOut>(`/pay/orders/${orderNo}`)
  },

  reconcile(orderNo: string, status: 'paying' | 'paid' | 'failed' = 'paid'): Promise<PayNotifyOut> {
    return apiPost<PayNotifyOut>(`/pay/orders/${orderNo}/reconcile`, { status })
  },
}
