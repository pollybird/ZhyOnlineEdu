import { apiGet, apiPost } from './request'
import type {
  QuizAttemptCreatedOut,
  QuizAttemptListOut,
  QuizAttemptOut,
  QuizEntryOut,
  QuizSubmitOut,
} from './types'

/** 学员端测验（M2.6 / M2.14）：答案永不下发；交卷必须带 Idempotency-Key。 */
export const quizApi = {
  entry(lessonId: number): Promise<QuizEntryOut> {
    return apiGet<QuizEntryOut>(`/lessons/${lessonId}/quiz`)
  },

  start(lessonId: number): Promise<QuizAttemptCreatedOut> {
    return apiPost<QuizAttemptCreatedOut>('/quiz/attempts', { lesson_id: lessonId })
  },

  attempt(attemptId: number): Promise<QuizAttemptOut> {
    return apiGet<QuizAttemptOut>(`/quiz/attempts/${attemptId}`)
  },

  submit(
    attemptId: number,
    attemptUid: string,
    answers: { question_id: number; choices: string[] }[],
  ): Promise<QuizSubmitOut> {
    return apiPost<QuizSubmitOut>(
      `/quiz/attempts/${attemptId}/submit`,
      { answers },
      { headers: { 'Idempotency-Key': attemptUid } },
    )
  },

  history(params: { lesson_id?: number; status?: string; page?: number }): Promise<QuizAttemptListOut> {
    return apiGet<QuizAttemptListOut>('/my/quiz/attempts', { params })
  },
}
