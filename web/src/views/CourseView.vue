<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { learnApi } from '../api/learn'
import type { LearnCourseDetailOut, LearnLessonOut } from '../api/types'
import PreviewBadge from '../components/PreviewBadge.vue'
import SourceTag from '../components/SourceTag.vue'
import StateView from '../components/StateView.vue'
import {
  accessLabel,
  describeError,
  formatDuration,
  formatPercent,
  formatRelative,
} from '../utils/format'
import type { ErrorView } from '../utils/format'
import { assetStatusLabel, courseModeLabel } from '../utils/labels'

/**
 * 学员端课程详情（M2.14）：章/课时大纲 + 逐课时进度 + 续播 / 试看提示。
 * 只展示 published 且模式可见的课程；草稿与模式不可见由后端统一 404。
 */
const route = useRoute()
const router = useRouter()

const courseId = computed(() => Number(route.params.courseId))
const loading = ref(true)
const error = ref<ErrorView | null>(null)
const detail = ref<LearnCourseDetailOut | null>(null)

const course = computed(() => detail.value?.course ?? null)
const lessons = computed<LearnLessonOut[]>(() =>
  (detail.value?.chapters ?? []).flatMap((chapter) => chapter.lessons),
)
const playable = computed(() => lessons.value.filter((item) => item.asset_status === 'encoded'))
const hasFreeLesson = computed(() => lessons.value.some((item) => item.is_free))
const resumeLesson = computed(() => {
  const id = course.value?.progress?.resume_lesson_id ?? null
  return id === null ? null : lessons.value.find((item) => item.lesson_id === id) ?? null
})
const state = computed(() => {
  if (loading.value) return 'loading'
  if (error.value !== null) return error.value.state
  return lessons.value.length === 0 ? 'empty' : 'ready'
})

async function load(): Promise<void> {
  loading.value = true
  error.value = null
  try {
    detail.value = await learnApi.course(courseId.value)
  } catch (caught) {
    error.value = describeError(caught)
  } finally {
    loading.value = false
  }
}

function openLesson(lesson: LearnLessonOut): void {
  if (lesson.asset_status !== 'encoded' || course.value === null) return
  void router.push({
    name: 'learn-player',
    params: { lessonId: lesson.lesson_id },
    query: { course: course.value.course_id },
  })
}

function openQuiz(lesson: LearnLessonOut): void {
  if (course.value === null || lesson.kind !== 'video') return
  void router.push({
    name: 'learn-quiz',
    params: { lessonId: lesson.lesson_id },
    query: { course: course.value.course_id },
  })
}

function start(): void {
  const target = resumeLesson.value ?? playable.value[0] ?? null
  if (target !== null) openLesson(target)
}

function handleErrorAction(): void {
  if (error.value?.action === 'member') {
    void router.push({ name: 'member' })
    return
  }
  if (error.value?.action === 'back') {
    void router.push({ name: 'home' })
    return
  }
  void load()
}

onMounted(() => {
  void load()
})
</script>

<template>
  <div class="course-page">
    <StateView
      :state="state"
      :title="error?.title"
      :message="error?.message"
      :code="error?.code"
      :action-text="error?.action === 'member' ? '去开通会员' : error?.action === 'back' ? '返回课程列表' : '重新加载'"
      @action="handleErrorAction"
    >
      <template v-if="course">
        <el-card class="course-hero">
          <div class="course-hero-main">
            <div class="course-hero-text">
              <div class="course-hero-title">
                <h2>{{ course.title }}</h2>
                <el-tag size="small" :type="course.access_ok ? 'success' : 'warning'">
                  {{ accessLabel(course.access_type) }}
                </el-tag>
                <el-tag size="small" type="info">{{ courseModeLabel(course.mode) }}</el-tag>
              </div>
              <p class="course-hero-summary">{{ course.summary || '暂无课程简介' }}</p>
              <div class="course-hero-meta">
                <span>{{ course.subject_name || '未分类' }}</span>
                <span>{{ course.total_lessons }} 课时</span>
                <span>{{ formatDuration(course.total_duration_sec) }}</span>
                <span v-if="course.progress">
                  {{ formatPercent(course.progress.percent) }} ·
                  {{ formatRelative(course.progress.last_watch_at) }}
                </span>
              </div>
            </div>
            <div class="course-hero-actions">
              <el-button
                type="primary"
                size="large"
                :disabled="playable.length === 0"
                @click="start"
              >
                {{ course.progress?.resume_lesson_id ? '继续学习' : '开始学习' }}
              </el-button>
              <el-progress
                v-if="course.progress"
                :percentage="Math.min(100, Math.round(course.progress.percent))"
                :stroke-width="10"
              />
            </div>
          </div>
          <div class="course-hero-source">
            <SourceTag
              :metric-version="detail?.metric_version ?? ''"
              :freshness-seconds="detail?.freshness_seconds"
              :generated-at="detail?.generated_at"
            />
          </div>
        </el-card>

        <el-alert
          v-if="!course.access_ok"
          class="course-alert"
          :type="course.access_type === 'member' ? 'warning' : 'info'"
          show-icon
          :closable="false"
          :title="course.access_type === 'member' ? '该课程需要开通会员' : '该课程仅限班级成员学习'"
        >
          <template #default>
            <p class="course-alert-text">
              {{
                course.access_type === 'member'
                  ? '开通会员后即可播放全部课时与参加测验。'
                  : '请联系老师加入班级后重新进入。'
              }}
            </p>
            <el-button
              v-if="course.access_type === 'member'"
              type="primary"
              size="small"
              @click="router.push({ name: 'member' })"
            >
              去开通会员
            </el-button>
          </template>
        </el-alert>

        <PreviewBadge
          v-if="!course.access_ok && hasFreeLesson"
          text="本课程含免费试看课时：试看播放不计入学习统计"
        />

        <el-alert
          v-if="playable.length === 0"
          class="course-alert"
          type="warning"
          show-icon
          :closable="false"
          title="课程暂无可播放课时"
        >
          课时仍在转码或尚未上传视频，完成转码后即可播放。
        </el-alert>

        <el-card
          v-for="chapter in detail?.chapters ?? []"
          :key="chapter.chapter_id"
          class="course-chapter"
        >
          <template #header>
            <span class="course-chapter-title">{{ chapter.title }}</span>
          </template>
          <div class="course-lesson-list">
            <div v-for="lesson in chapter.lessons" :key="lesson.lesson_id" class="course-lesson">
              <div class="course-lesson-main" @click="openLesson(lesson)">
                <span class="course-lesson-title">{{ lesson.title }}</span>
                <span class="course-lesson-meta">
                  {{ formatDuration(lesson.duration_sec) }}
                  <el-tag v-if="lesson.is_free" size="small" type="warning">试看</el-tag>
                  <el-tag
                    v-if="lesson.asset_status !== 'encoded'"
                    size="small"
                    type="info"
                  >
                    {{ assetStatusLabel(lesson.asset_status) }}
                  </el-tag>
                  <el-tag v-if="lesson.progress?.completed" size="small" type="success">
                    已完成
                  </el-tag>
                  <el-tag
                    v-else-if="lesson.progress"
                    size="small"
                    type="primary"
                  >
                    {{ formatPercent(lesson.progress.percent) }}
                  </el-tag>
                </span>
              </div>
              <div class="course-lesson-actions">
                <el-button
                  link
                  type="primary"
                  :disabled="lesson.asset_status !== 'encoded'"
                  @click="openLesson(lesson)"
                >
                  {{ lesson.progress ? '继续播放' : '播放' }}
                </el-button>
                <el-button
                  v-if="lesson.kind === 'video'"
                  link
                  type="primary"
                  @click="openQuiz(lesson)"
                >
                  测验
                </el-button>
              </div>
            </div>
          </div>
        </el-card>
        <el-empty v-if="lessons.length === 0" description="课程尚未配置课时" />
      </template>
    </StateView>
  </div>
</template>
