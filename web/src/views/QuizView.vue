<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { learnApi } from '../api/learn'
import { quizApi } from '../api/quiz'
import { ApiError } from '../api/request'
import type { LearnCourseDetailOut, QuizAttemptOut, QuizEntryOut } from '../api/types'
import StateView from '../components/StateView.vue'
import { describeError, formatPercent } from '../utils/format'
import { questionKindLabel } from '../utils/labels'
import type { ErrorView } from '../utils/format'

/**
 * 学员端测验页（M2.14 · M2.6 口径）。
 * - 题面永不含标准答案；交卷必须带 `Idempotency-Key: attempt_uid`；
 * - 同一课时已有进行中作答时不重复扣题（created=false）；
 * - 交卷后展示逐题对错与解析，支持再考一次。
 */
const route = useRoute()
const router = useRouter()

const lessonId = computed(() => Number(route.params.lessonId))
const courseId = computed(() => {
  const raw = route.query.course
  if (typeof raw !== 'string' || raw.trim() === '') return null
  const value = Number(raw)
  return Number.isInteger(value) && value > 0 ? value : null
})

const loading = ref(true)
const error = ref<ErrorView | null>(null)
const entry = ref<QuizEntryOut | null>(null)
const attempt = ref<QuizAttemptOut | null>(null)
const detail = ref<LearnCourseDetailOut | null>(null)
const selections = ref<Record<number, string[]>>({})
const busy = ref(false)
const submitted = ref(false)

const lessonTitle = computed(() => {
  const row = (detail.value?.chapters ?? [])
    .flatMap((chapter) => chapter.lessons)
    .find((item) => item.lesson_id === lessonId.value)
  return row?.title ?? `课时 #${lessonId.value}`
})
const questions = computed(() => attempt.value?.questions ?? [])
const resultMap = computed(() => {
  const rows = attempt.value?.result ?? []
  return new Map(rows.map((row) => [row.question_id, row]))
})
const state = computed(() => {
  if (loading.value) return 'loading'
  if (error.value !== null) return error.value.state
  return attempt.value === null ? 'empty' : 'ready'
})

function isQuizEmpty(data: unknown): boolean {
  return (
    typeof data === 'object' &&
    data !== null &&
    'reason' in data &&
    (data as { reason?: unknown }).reason === 'quiz_empty'
  )
}

async function load(): Promise<void> {
  loading.value = true
  error.value = null
  attempt.value = null
  submitted.value = false
  selections.value = {}
  try {
    const tasks: [Promise<QuizEntryOut>, Promise<LearnCourseDetailOut> | null] = [
      quizApi.entry(lessonId.value),
      courseId.value === null ? null : learnApi.course(courseId.value),
    ]
    const [payload, course] = await Promise.all([
      tasks[0],
      tasks[1] ?? Promise.resolve(null),
    ])
    entry.value = payload
    detail.value = course
    if (payload.attempt !== null) {
      attempt.value = payload.attempt
      if (payload.attempt.status !== 'in_progress') submitted.value = true
    }
  } catch (caught) {
    if (caught instanceof ApiError && caught.code === 'NOT_FOUND' && isQuizEmpty(caught.data)) {
      error.value = {
        state: 'empty',
        code: 'quiz_empty',
        title: '本课时暂无测验',
        message: '老师尚未配置题目，可先观看课时内容',
        action: 'back',
      }
    } else {
      error.value = describeError(caught)
    }
  } finally {
    loading.value = false
  }
}

async function start(): Promise<void> {
  busy.value = true
  error.value = null
  try {
    const payload = await quizApi.start(lessonId.value)
    attempt.value = payload.attempt
    submitted.value = false
    selections.value = {}
  } catch (caught) {
    error.value = describeError(caught)
  } finally {
    busy.value = false
  }
}

function setSingle(questionId: number, value: unknown): void {
  const text = String(value ?? '')
  selections.value = {
    ...selections.value,
    [questionId]: text === '' ? [] : [text],
  }
}

function setMulti(questionId: number, value: unknown): void {
  const list = Array.isArray(value) ? value.map((item) => String(item)) : []
  selections.value = { ...selections.value, [questionId]: list }
}

async function submit(): Promise<void> {
  const current = attempt.value
  if (current === null || busy.value) return
  busy.value = true
  error.value = null
  try {
    const answers = current.questions.map((question) => ({
      question_id: question.question_id,
      choices: selections.value[question.question_id] ?? [],
    }))
    const payload = await quizApi.submit(current.id, current.attempt_uid, answers)
    attempt.value = payload.attempt
    submitted.value = true
  } catch (caught) {
    error.value = describeError(caught)
  } finally {
    busy.value = false
  }
}

function handleErrorAction(): void {
  if (error.value?.action === 'member') {
    void router.push({ name: 'member' })
    return
  }
  if (error.value?.action === 'back' || courseId.value === null) {
    if (courseId.value !== null) {
      void router.push({ name: 'learn-course', params: { courseId: courseId.value } })
    } else {
      void router.push({ name: 'home' })
    }
    return
  }
  void load()
}

function back(): void {
  if (courseId.value === null) {
    void router.push({ name: 'home' })
    return
  }
  void router.push({ name: 'learn-course', params: { courseId: courseId.value } })
}

onMounted(() => {
  void load()
})
</script>

<template>
  <div class="quiz-page">
    <el-card class="quiz-head">
      <div class="quiz-head-row">
        <div>
          <h2 class="quiz-title">{{ lessonTitle }}</h2>
          <p class="quiz-sub">
            <span v-if="entry">共 {{ entry.question_count }} 题 · 总分 {{ entry.score_total }}</span>
            <el-tag v-if="entry" size="small" type="info">及格线 {{ entry.pass_score }}%</el-tag>
            <el-tag v-if="entry?.last" size="small" type="success">
              最近得分 {{ formatPercent(entry.last.percent) }}
            </el-tag>
          </p>
        </div>
        <el-button @click="back">返回课程</el-button>
      </div>
    </el-card>

    <StateView
      :state="state"
      :title="error?.title"
      :message="error?.message"
      :code="error?.code"
      action-text="返回课程"
      @action="handleErrorAction"
    >
      <el-card v-if="attempt && !submitted" class="quiz-body">
        <template #header>
          <div class="quiz-head-row">
            <span>答题中</span>
            <el-tag size="small" type="warning">交卷后不可修改</el-tag>
          </div>
        </template>

        <div v-for="(question, index) in questions" :key="question.question_id" class="quiz-item">
          <p class="quiz-stem">
            <span class="quiz-index">{{ index + 1 }}.</span>
            {{ question.stem }}
            <el-tag size="small" type="info">{{ question.score }} 分</el-tag>
            <el-tag size="small">{{ questionKindLabel(question.kind) }}</el-tag>
          </p>

          <el-radio-group
            v-if="question.kind !== 'multiple'"
            :model-value="selections[question.question_id]?.[0] ?? ''"
            @change="setSingle(question.question_id, $event)"
          >
            <el-radio
              v-for="option in question.options"
              :key="option.key"
              :value="option.key"
              class="quiz-option"
            >
              {{ option.key }}. {{ option.text }}
            </el-radio>
          </el-radio-group>

          <el-checkbox-group
            v-else
            :model-value="selections[question.question_id] ?? []"
            @change="setMulti(question.question_id, $event)"
          >
            <el-checkbox
              v-for="option in question.options"
              :key="option.key"
              :value="option.key"
              class="quiz-option"
            >
              {{ option.key }}. {{ option.text }}
            </el-checkbox>
          </el-checkbox-group>
        </div>

        <div class="quiz-actions">
          <el-button type="primary" :loading="busy" @click="submit">交卷</el-button>
          <span class="quiz-hint">未作答按 0 分；多选需完全正确才得分。</span>
        </div>
      </el-card>

      <el-card v-if="attempt && submitted" class="quiz-body">
        <template #header>
          <div class="quiz-head-row">
            <span>测验结果</span>
            <el-tag size="small" :type="attempt.passed ? 'success' : 'danger'">
              {{ attempt.passed ? '通过' : '未通过' }}
            </el-tag>
          </div>
        </template>

        <el-result
          :icon="attempt.passed ? 'success' : 'warning'"
          :title="`${formatPercent(attempt.percent)} · ${attempt.score_earned}/${attempt.score_total}`"
          :sub-title="attempt.passed ? '已通过本次测验' : '未达及格线，可再考一次'"
        />

        <div v-for="(question, index) in questions" :key="question.question_id" class="quiz-item">
          <p class="quiz-stem">
            <span class="quiz-index">{{ index + 1 }}.</span>
            {{ question.stem }}
            <el-tag
              size="small"
              :type="resultMap.get(question.question_id)?.correct ? 'success' : 'danger'"
            >
              {{ resultMap.get(question.question_id)?.correct ? '正确' : '错误' }}
            </el-tag>
            <el-tag size="small" type="info">
              {{ resultMap.get(question.question_id)?.score_earned ?? 0 }} /
              {{ question.score }} 分
            </el-tag>
          </p>
          <p
            v-if="resultMap.get(question.question_id)?.explanation"
            class="quiz-explain"
          >
            解析：{{ resultMap.get(question.question_id)?.explanation }}
          </p>
        </div>

        <div class="quiz-actions">
          <el-button type="primary" :loading="busy" @click="start">再考一次</el-button>
          <el-button @click="back">返回课程</el-button>
        </div>
      </el-card>

      <el-card v-if="attempt === null && entry" class="quiz-body">
        <el-empty description="还没有正在进行的作答">
          <el-button type="primary" :loading="busy" @click="start">开始测验</el-button>
        </el-empty>
      </el-card>
    </StateView>
  </div>
</template>
