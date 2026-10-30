import { apiDelete, apiGet, apiPatch, apiPost } from './request'
import type {
  ChapterOut,
  ChapterReorderOut,
  CourseCreateIn,
  CourseDetailOut,
  CourseListOut,
  CourseOut,
  CoursePublishOut,
  CourseReorderOut,
  CourseUnpublishOut,
  CourseUpdateIn,
  DeleteOut,
  LessonCreateIn,
  LessonOut,
  LessonUpdateIn,
  SubjectCreateIn,
  SubjectListOut,
  SubjectOut,
  SubjectUpdateIn,
} from './types'

/** 学科树 + 课程三层（M2.1）；写操作一律携带 version（乐观锁）。 */
export const catalogApi = {
  subjects(): Promise<SubjectListOut> {
    return apiGet<SubjectListOut>('/subjects')
  },
  createSubject(body: SubjectCreateIn): Promise<SubjectOut> {
    return apiPost<SubjectOut>('/subjects', body)
  },
  updateSubject(id: number, body: SubjectUpdateIn): Promise<SubjectOut> {
    return apiPatch<SubjectOut>(`/subjects/${id}`, body)
  },
  deleteSubject(id: number, version: number): Promise<DeleteOut> {
    return apiDelete<DeleteOut>(`/subjects/${id}`, { version })
  },

  courses(params: {
    subject_id?: number
    status?: string
    keyword?: string
    page?: number
    page_size?: number
  }): Promise<CourseListOut> {
    return apiGet<CourseListOut>('/courses', { params })
  },
  createCourse(body: CourseCreateIn): Promise<CourseOut> {
    return apiPost<CourseOut>('/courses', body)
  },
  course(id: number): Promise<CourseDetailOut> {
    return apiGet<CourseDetailOut>(`/courses/${id}`)
  },
  updateCourse(id: number, body: CourseUpdateIn): Promise<CourseOut> {
    return apiPatch<CourseOut>(`/courses/${id}`, body)
  },
  deleteCourse(id: number, version: number): Promise<DeleteOut> {
    return apiDelete<DeleteOut>(`/courses/${id}`, { version })
  },
  publishCourse(id: number, version: number): Promise<CoursePublishOut> {
    return apiPost<CoursePublishOut>(`/courses/${id}/publish`, { version })
  },
  unpublishCourse(id: number, version: number): Promise<CourseUnpublishOut> {
    return apiPost<CourseUnpublishOut>(`/courses/${id}/unpublish`, { version })
  },
  reorderChapters(id: number, version: number, orderedIds: number[]): Promise<CourseReorderOut> {
    return apiPost<CourseReorderOut>(`/courses/${id}/reorder`, {
      version,
      ordered_ids: orderedIds,
    })
  },

  createChapter(courseId: number, title: string): Promise<ChapterOut> {
    return apiPost<ChapterOut>(`/courses/${courseId}/chapters`, { title })
  },
  updateChapter(id: number, body: { version: number; title?: string }): Promise<ChapterOut> {
    return apiPatch<ChapterOut>(`/chapters/${id}`, body)
  },
  deleteChapter(id: number, version: number): Promise<DeleteOut> {
    return apiDelete<DeleteOut>(`/chapters/${id}`, { version })
  },
  reorderLessons(
    chapterId: number,
    version: number,
    orderedIds: number[],
  ): Promise<ChapterReorderOut> {
    return apiPost<ChapterReorderOut>(`/chapters/${chapterId}/reorder`, {
      version,
      ordered_ids: orderedIds,
    })
  },

  createLesson(chapterId: number, body: LessonCreateIn): Promise<LessonOut> {
    return apiPost<LessonOut>(`/chapters/${chapterId}/lessons`, body)
  },
  updateLesson(id: number, body: LessonUpdateIn): Promise<LessonOut> {
    return apiPatch<LessonOut>(`/lessons/${id}`, body)
  },
  deleteLesson(id: number, version: number): Promise<DeleteOut> {
    return apiDelete<DeleteOut>(`/lessons/${id}`, { version })
  },
}
