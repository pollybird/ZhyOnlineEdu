<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { learnApi } from '../api/learn'
import type { LearnCourseListOut, MySummaryOut } from '../api/types'
import StateView from '../components/StateView.vue'
import SourceTag from '../components/SourceTag.vue'
import ShowcaseTips from '../components/ShowcaseTips.vue'
import {
  accessLabel,
  describeError,
  formatDuration,
  formatPercent,
  formatRelative,
} from '../utils/format'
import type { ErrorView } from '../utils/format'

/**
 * 学员端首页（M2.14）：首屏 ≤2 请求（`/my/summary` + `/learn/courses`，§23.5）。
 * 展示学习概览、最近课程、课程浏览与测验统计；空/错误/无权限/加载/试看五态齐全。
 */
const router = useRouter()
const loading = ref(true)
const error = ref<ErrorView | null>(null)
const summary = ref<MySummaryOut | null>(null)
const catalog = ref<LearnCourseListOut | null>(null)
const keyword = ref('')

const state = computed(() => {
  if (loading.value) return 'loading'
  if (error.value !== null) return error.value.state
  const items = catalog.value?.items ?? []
  return items.length === 0 ? 'empty' : 'ready'
})

const recent = computed(() => summary.value?.recent_courses ?? [])

async function load(): Promise<void> {
  loading.value = true
  error.value = null
  try {
    const [overview, courses] = await Promise.all([
      learnApi.summary(),
      learnApi.courses({
        keyword: keyword.value.trim() === '' ? undefined : keyword.value.trim(),
        page_size: 12,
      }),
    ])
    summary.value = overview
    catalog.value = courses
  } catch (caught) {
    error.value = describeError(caught)
  } finally {
    loading.value = false
  }
}

function openCourse(courseId: number): void {
  void router.push({ name: 'learn-course', params: { courseId } })
}

function resume(courseId: number, lessonId: number | null): void {
  if (lessonId === null) {
    openCourse(courseId)
    return
  }
  void router.push({ name: 'learn-player', params: { lessonId }, query: { course: courseId } })
}

function handleErrorAction(): void {
  if (error.value?.action === 'member') {
    void router.push({ name: 'member' })
    return
  }
  void load()
}

onMounted(() => {
  void load()
})
</script>

<template>
  <div class="learn-home">
    <ShowcaseTips
      title="学习中心"
      description="从这里继续上次的学习，或浏览全部已上架课程。"
    />

    <StateView
      :state="state"
      :title="error?.title"
      :message="error?.message"
      :code="error?.code"
      :action-text="error?.action === 'member' ? '去开通会员' : '重新加载'"
      @action="handleErrorAction"
    >
      <el-card v-if="summary" class="learn-overview">
        <template #header>
          <div class="learn-head">
            <span>学习概览</span>
            <SourceTag
              :metric-version="summary.metric_version"
              :freshness-seconds="summary.freshness_seconds"
              :generated-at="summary.generated_at"
            />
          </div>
        </template>
        <el-row :gutter="16">
          <el-col :xs="12" :sm="6">
            <div class="learn-stat">
              <span class="learn-stat-value">{{ summary.courses_started }}</span>
              <span class="learn-stat-label">已学课程</span>
            </div>
          </el-col>
          <el-col :xs="12" :sm="6">
            <div class="learn-stat">
              <span class="learn-stat-value">{{ summary.courses_completed }}</span>
              <span class="learn-stat-label">已完成课程</span>
            </div>
          </el-col>
          <el-col :xs="12" :sm="6">
            <div class="learn-stat">
              <span class="learn-stat-value">{{ formatDuration(summary.today_watch_seconds) }}</span>
              <span class="learn-stat-label">今日有效学习</span>
            </div>
          </el-col>
          <el-col :xs="12" :sm="6">
            <div class="learn-stat">
              <span class="learn-stat-value">{{ summary.lessons_completed }}</span>
              <span class="learn-stat-label">完成课时</span>
            </div>
          </el-col>
        </el-row>
        <div class="learn-quiz">
          <el-tag size="small" type="info">测验 {{ summary.quiz.attempts }} 次</el-tag>
          <el-tag size="small" type="success">通过 {{ summary.quiz.passed }} 次</el-tag>
          <el-tag size="small">平均 {{ formatPercent(summary.quiz.average_percent) }}</el-tag>
          <el-tag size="small" type="warning">最佳 {{ formatPercent(summary.quiz.best_percent) }}</el-tag>
        </div>
      </el-card>

      <el-card v-if="recent.length > 0" class="learn-recent">
        <template #header>继续学习</template>
        <div class="learn-recent-list">
          <div v-for="item in recent" :key="item.course_id" class="learn-recent-item">
            <div class="learn-recent-main">
              <span class="learn-recent-title">{{ item.course_title }}</span>
              <span class="learn-recent-meta">
                {{ formatPercent(item.percent) }} · {{ formatRelative(item.last_watch_at) }}
              </span>
            </div>
            <el-progress
              :percentage="Math.min(100, Math.round(item.percent))"
              :stroke-width="8"
              :show-text="false"
            />
            <el-button
              type="primary"
              size="small"
              @click="resume(item.course_id, item.resume_lesson_id)"
            >
              {{ item.resume_lesson_id === null ? '去学习' : '继续播放' }}
            </el-button>
          </div>
        </div>
      </el-card>

      <el-card class="learn-catalog">
        <template #header>
          <div class="learn-head">
            <span>课程浏览</span>
            <div class="learn-search">
              <el-input
                v-model="keyword"
                size="small"
                placeholder="搜索课程标题"
                clearable
                @keyup.enter="load"
                @clear="load"
              />
              <el-button size="small" @click="load">搜索</el-button>
            </div>
          </div>
        </template>

        <el-row v-if="catalog" :gutter="16">
          <el-col
            v-for="course in catalog.items"
            :key="course.course_id"
            :xs="24"
            :sm="12"
            :lg="8"
          >
            <el-card class="learn-course-card" shadow="hover" @click="openCourse(course.course_id)">
              <div class="learn-course-head">
                <span class="learn-course-title">{{ course.title }}</span>
                <el-tag size="small" :type="course.access_ok ? 'success' : 'warning'">
                  {{ accessLabel(course.access_type) }}
                </el-tag>
              </div>
              <p class="learn-course-summary">{{ course.summary || '暂无简介' }}</p>
              <div class="learn-course-meta">
                <span>{{ course.subject_name || '未分类' }}</span>
                <span>{{ course.total_lessons }} 课时</span>
                <span>{{ formatDuration(course.total_duration_sec) }}</span>
              </div>
              <el-progress
                v-if="course.progress"
                :percentage="Math.min(100, Math.round(course.progress.percent))"
                :stroke-width="8"
                :format="(value: number) => `${value}%`"
              />
              <div v-if="course.progress" class="learn-course-foot">
                <span>{{ formatRelative(course.progress.last_watch_at) }}</span>
                <el-button link type="primary" @click.stop="resume(course.course_id, course.progress?.resume_lesson_id ?? null)">
                  {{ course.progress.resume_lesson_id === null ? '开始学习' : '继续学习' }}
                </el-button>
              </div>
              <div v-else class="learn-course-foot">
                <span>尚未开始</span>
                <el-button link type="primary" @click.stop="openCourse(course.course_id)">
                  查看课程
                </el-button>
              </div>
            </el-card>
          </el-col>
        </el-row>
        <el-empty v-if="catalog && catalog.items.length === 0" description="暂无已上架课程" />
        <p v-if="catalog" class="learn-hint">
          共 {{ catalog.total }} 门课程
        </p>
      </el-card>
    </StateView>
  </div>
</template>
