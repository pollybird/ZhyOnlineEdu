<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { learnApi, reportProgressKeepalive } from '../api/learn'
import type { LearnCourseDetailOut, LearnLessonOut, TicketOut } from '../api/types'
import PreviewBadge from '../components/PreviewBadge.vue'
import StateView from '../components/StateView.vue'
import { ProgressReporter } from '../media/ProgressReporter'
import { TicketedHls, type PlayerStatus } from '../media/TicketedHls'
import { describeError, formatDuration, formatPercent } from '../utils/format'
import type { ErrorView } from '../utils/format'

/**
 * 学员端播放页（M2.14 · §23.3/§23.4）。
 *
 * - 播放只走 `TicketedHls`（换票 + 原生 HLS query 票据）；
 * - `ProgressReporter` 30s 心跳 + `visibilitychange`/`pagehide` 兜底；
 * - 202 即成功：UI 显示「进度已保存」，不转圈等待投影；
 * - 预览态（超管/教师）展示 `PreviewBadge`，只落流水不计入统计。
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
const detail = ref<LearnCourseDetailOut | null>(null)
const ticket = ref<TicketOut | null>(null)
const status = ref<PlayerStatus>('idle')
const preview = ref(false)
const saved = ref(false)
const savedError = ref('')
const videoRef = ref<HTMLVideoElement | null>(null)
const profile = ref('')
const switchSupported = ref(false)

let player: TicketedHls | null = null
let reporter: ProgressReporter | null = null

const lessons = computed<LearnLessonOut[]>(() =>
  (detail.value?.chapters ?? []).flatMap((chapter) => chapter.lessons),
)
const lesson = computed<LearnLessonOut | null>(
  () => lessons.value.find((item) => item.lesson_id === lessonId.value) ?? null,
)
const nextLesson = computed<LearnLessonOut | null>(() => {
  const index = lessons.value.findIndex((item) => item.lesson_id === lessonId.value)
  if (index < 0) return null
  return (
    lessons.value
      .slice(index + 1)
      .find((item) => item.asset_status === 'encoded') ?? null
  )
})
const state = computed(() => {
  if (loading.value) return 'loading'
  if (error.value !== null) return error.value.state
  return 'ready'
})
const profiles = computed(() => ticket.value?.profiles ?? [])

function teardown(): void {
  if (reporter !== null) {
    void reporter.flush('hide')
    reporter.detach()
    reporter = null
  }
  player?.destroy()
  player = null
  ticket.value = null
  preview.value = false
  saved.value = false
  savedError.value = ''
  status.value = 'idle'
  profile.value = ''
  switchSupported.value = false
}

async function load(): Promise<void> {
  teardown()
  loading.value = true
  error.value = null
  detail.value = null
  try {
    if (courseId.value === null) {
      error.value = describeError({ code: 'NOT_FOUND' })
      return
    }
    const payload = await learnApi.course(courseId.value)
    detail.value = payload
    const row = payload.chapters
      .flatMap((chapter) => chapter.lessons)
      .find((item) => item.lesson_id === lessonId.value)
    if (row === undefined) {
      error.value = describeError({ code: 'NOT_FOUND' })
      return
    }
    if (row.asset_status !== 'encoded') {
      error.value = describeError({ code: 40202 })
      return
    }
    loading.value = false
    await nextTick()
    const video = videoRef.value
    if (video === null || courseId.value === null) return
    const instance = new TicketedHls(video, {
      lessonId: lessonId.value,
      fetchTicket: () => learnApi.ticket(lessonId.value),
      onStatus: (value) => {
        status.value = value
      },
      onTicket: (value) => {
        ticket.value = value
        preview.value = value.preview
      },
      onFatal: (caught) => {
        error.value = describeError(caught)
      },
    })
    player = instance
    const issued = await instance.load()
    switchSupported.value = instance.supportsProfileSwitch
    reporter = new ProgressReporter({
      lessonId: lessonId.value,
      send: (payload, reason) =>
        reason === 'hide'
          ? reportProgressKeepalive(payload)
          : learnApi.reportProgress(payload),
      onSaved: () => {
        saved.value = true
        savedError.value = ''
      },
      onError: (caught) => {
        saved.value = false
        savedError.value = describeError(caught).message
      },
    })
    reporter.attach(video, issued.resume_second)
  } catch (caught) {
    error.value = describeError(caught)
  } finally {
    loading.value = false
  }
}

function onTimeUpdate(): void {
  reporter?.track()
}

function handleErrorAction(): void {
  if (error.value?.action === 'member') {
    void router.push({ name: 'member' })
    return
  }
  if (error.value?.action === 'back' || courseId.value === null) {
    void router.push({ name: 'home' })
    return
  }
  void load()
}

function backToCourse(): void {
  if (courseId.value === null) return
  void router.push({ name: 'learn-course', params: { courseId: courseId.value } })
}

function openQuiz(): void {
  if (courseId.value === null) return
  void router.push({
    name: 'learn-quiz',
    params: { lessonId: lessonId.value },
    query: { course: courseId.value },
  })
}

async function openNext(): Promise<void> {
  const target = nextLesson.value
  if (target === null || courseId.value === null) return
  await reporter?.flush('hide')
  void router.push({
    name: 'learn-player',
    params: { lessonId: target.lesson_id },
    query: { course: courseId.value },
  })
}

function changeProfile(value: string | number | boolean | undefined): void {
  const next = typeof value === 'string' && value !== '' ? value : null
  player?.setProfile(next)
}

watch(
  () => route.params.lessonId,
  () => {
    void load()
  },
)

onMounted(() => {
  void load()
})

onUnmounted(() => {
  teardown()
})
</script>

<template>
  <div class="player-page">
    <StateView
      :state="state"
      :title="error?.title"
      :message="error?.message"
      :code="error?.code"
      :action-text="error?.action === 'member' ? '去开通会员' : '返回课程'"
      @action="handleErrorAction"
    >
      <div class="player-layout">
        <el-card class="player-card">
          <template #header>
            <div class="player-head">
              <el-button link @click="backToCourse">← 返回课程</el-button>
              <span class="player-title">{{ lesson?.title ?? '课时播放' }}</span>
              <el-button link type="primary" @click="openQuiz">测验</el-button>
            </div>
          </template>

          <PreviewBadge v-if="preview" />

          <div class="player-stage">
            <video
              ref="videoRef"
              class="player-video"
              controls
              playsinline
              preload="metadata"
              @timeupdate="onTimeUpdate"
            />
            <div v-if="status === 'loading'" class="player-mask">正在准备视频…</div>
            <div v-else-if="status === 'refreshing'" class="player-mask">正在更新播放凭证…</div>
            <div v-else-if="status === 'error'" class="player-mask player-mask-error">
              播放中断，请重试
            </div>
          </div>

          <div class="player-status">
            <el-tag v-if="saved" size="small" type="success">进度已保存</el-tag>
            <el-tag v-else size="small" type="info">每 30 秒自动保存进度</el-tag>
            <el-tag v-if="status === 'ready'" size="small" type="success">播放就绪</el-tag>
            <span v-if="savedError" class="player-save-error">{{ savedError }}</span>
            <div v-if="switchSupported && profiles.length > 0" class="player-quality">
              <span>清晰度</span>
              <el-radio-group v-model="profile" size="small" @change="changeProfile">
                <el-radio-button value="">自动</el-radio-button>
                <el-radio-button v-for="item in profiles" :key="item" :value="item">
                  {{ item }}P
                </el-radio-button>
              </el-radio-group>
            </div>
          </div>
        </el-card>

        <el-card v-if="lesson" class="player-side">
          <template #header>本课时</template>
          <el-descriptions :column="1" border>
            <el-descriptions-item label="时长">
              {{ formatDuration(lesson.duration_sec) }}
            </el-descriptions-item>
            <el-descriptions-item label="已完成学习">
              {{ lesson.progress ? formatPercent(lesson.progress.percent) : '尚未开始' }}
            </el-descriptions-item>
            <el-descriptions-item label="上次位置">
              {{ lesson.progress ? formatDuration(lesson.progress.last_position) : '—' }}
            </el-descriptions-item>
          </el-descriptions>
          <div class="player-side-actions">
            <el-button type="primary" :disabled="nextLesson === null" @click="openNext">
              {{ nextLesson === null ? '已是最后一个课时' : '下一课时' }}
            </el-button>
            <el-button @click="openQuiz">去做测验</el-button>
          </div>
          <p class="player-hint">
            切后台/锁屏会自动补报；回到页面后进度不会回跳。
          </p>
        </el-card>
      </div>
    </StateView>
  </div>
</template>
