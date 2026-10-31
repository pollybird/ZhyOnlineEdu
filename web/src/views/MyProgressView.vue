<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { learnApi } from '../api/learn'
import type {
  CourseProgressDetailOut,
  CourseProgressListOut,
  ProgressGateOut,
} from '../api/types'
import SourceTag from '../components/SourceTag.vue'
import StateView from '../components/StateView.vue'
import {
  describeError,
  formatDuration,
  formatPercent,
  formatRelative,
} from '../utils/format'
import type { ErrorView } from '../utils/format'

/**
 * 我的学习档案（M2.14 · §10.7）。
 * - 列表 + 课程详情抽屉（仅已学课时）；
 * - 统计口径：只计入真实播放，试看与预览不计入；数据同步可能有短暂延迟。
 */
const router = useRouter()

const loading = ref(true)
const error = ref<ErrorView | null>(null)
const list = ref<CourseProgressListOut | null>(null)
const gate = ref<ProgressGateOut | null>(null)
const status = ref('')
const keyword = ref('')
const page = ref(1)
const detail = ref<CourseProgressDetailOut | null>(null)
const detailLoading = ref(false)
const detailError = ref<ErrorView | null>(null)

const state = computed(() => {
  if (loading.value) return 'loading'
  if (error.value !== null) return error.value.state
  return (list.value?.items.length ?? 0) === 0 ? 'empty' : 'ready'
})
const detailState = computed(() => {
  if (detailLoading.value) return 'loading'
  if (detailError.value !== null) return detailError.value.state
  return 'ready'
})

async function load(): Promise<void> {
  loading.value = true
  error.value = null
  try {
    const [payload, gateInfo] = await Promise.all([
      learnApi.myProgress({
        status: status.value === '' ? undefined : status.value,
        keyword: keyword.value.trim() === '' ? undefined : keyword.value.trim(),
        page: page.value,
        page_size: 12,
      }),
      learnApi.gate(),
    ])
    list.value = payload
    gate.value = gateInfo
  } catch (caught) {
    error.value = describeError(caught)
  } finally {
    loading.value = false
  }
}

async function openDetail(courseId: number): Promise<void> {
  detail.value = null
  detailError.value = null
  detailLoading.value = true
  try {
    detail.value = await learnApi.myProgressDetail(courseId)
  } catch (caught) {
    detailError.value = describeError(caught)
  } finally {
    detailLoading.value = false
  }
}

function resume(courseId: number, lessonId: number | null): void {
  if (lessonId === null) {
    void router.push({ name: 'learn-course', params: { courseId } })
    return
  }
  void router.push({
    name: 'learn-player',
    params: { lessonId },
    query: { course: courseId },
  })
}

function changePage(next: number): void {
  page.value = next
  void load()
}

function closeDetail(): void {
  detail.value = null
  detailError.value = null
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
  <div class="progress-page">
    <el-card class="progress-head">
      <div class="progress-head-row">
        <div>
          <h2 class="progress-title">我的学习档案</h2>
          <p class="progress-sub">
            统计只计入真实播放，试看与预览不计入；学习数据同步可能有短暂延迟。
          </p>
        </div>
        <div class="progress-filters">
          <el-select v-model="status" size="small" placeholder="全部状态" clearable @change="load">
            <el-option label="进行中" value="in_progress" />
            <el-option label="已完成" value="completed" />
          </el-select>
          <el-input
            v-model="keyword"
            size="small"
            placeholder="按课程标题搜索"
            clearable
            @keyup.enter="load"
            @clear="load"
          />
          <el-button size="small" @click="load">搜索</el-button>
        </div>
      </div>

      <div v-if="list" class="progress-source">
        <SourceTag
          :metric-version="list.metric_version"
          :freshness-seconds="list.freshness_seconds"
          :generated-at="list.generated_at"
        />
        <el-tag
          v-if="gate && !gate.healthy"
          size="small"
          type="warning"
        >
          统计数据同步中，可能稍有延迟
        </el-tag>
      </div>
    </el-card>

    <StateView
      :state="state"
      :title="error?.title"
      :message="error?.message"
      :code="error?.code"
      action-text="重新加载"
      @action="handleErrorAction"
    >
      <div class="progress-grid">
        <el-card v-for="item in list?.items ?? []" :key="item.course_id" class="progress-card">
          <div class="progress-card-head">
            <span class="progress-card-title">{{ item.course_title }}</span>
            <el-tag size="small" :type="item.completed ? 'success' : 'primary'">
              {{ item.completed ? '已完成' : formatPercent(item.percent) }}
            </el-tag>
          </div>
          <p class="progress-card-meta">
            {{ item.subject_name || '未分类' }} · {{ item.completed_lessons }}/{{ item.total_lessons }} 课时
          </p>
          <el-progress
            :percentage="Math.min(100, Math.round(item.percent))"
            :stroke-width="8"
            :show-text="false"
          />
          <div class="progress-card-stats">
            <span>观看 {{ formatDuration(item.watch_seconds) }}</span>
            <span>有效 {{ formatDuration(item.speed_time) }}</span>
            <span>{{ formatRelative(item.last_watch_at) }}</span>
          </div>
          <div class="progress-card-actions">
            <el-button size="small" @click="openDetail(item.course_id)">课时明细</el-button>
            <el-button
              type="primary"
              size="small"
              @click="resume(item.course_id, item.resume_lesson_id)"
            >
              {{ item.completed ? '再学一次' : '继续学习' }}
            </el-button>
          </div>
        </el-card>
      </div>

      <div v-if="list && list.total > list.page_size" class="progress-pager">
        <el-button size="small" :disabled="page <= 1" @click="changePage(page - 1)">上一页</el-button>
        <span class="progress-pager-text">
          第 {{ list.page }} 页 · 共 {{ list.total }} 门
        </span>
        <el-button
          size="small"
          :disabled="page * list.page_size >= list.total"
          @click="changePage(page + 1)"
        >
          下一页
        </el-button>
      </div>
    </StateView>

    <el-drawer
      :model-value="detailLoading || detail !== null || detailError !== null"
      :title="detail?.course.course_title ?? '课时明细'"
      size="62%"
      @close="closeDetail"
    >
      <StateView
        :state="detailState"
        :title="detailError?.title"
        :message="detailError?.message"
        :code="detailError?.code"
        action-text="关闭"
        @action="closeDetail"
      >
        <template v-if="detail">
          <div class="progress-source">
            <SourceTag
              :metric-version="detail.metric_version"
              :freshness-seconds="detail.freshness_seconds"
              :generated-at="detail.generated_at"
            />
          </div>
          <el-descriptions :column="2" border class="progress-detail-desc">
            <el-descriptions-item label="进度">
              {{ formatPercent(detail.course.percent) }}
            </el-descriptions-item>
            <el-descriptions-item label="已完成">
              {{ detail.course.completed_lessons }}/{{ detail.course.total_lessons }}
            </el-descriptions-item>
            <el-descriptions-item label="观看时长">
              {{ formatDuration(detail.course.watch_seconds) }}
            </el-descriptions-item>
            <el-descriptions-item label="最近学习">
              {{ formatRelative(detail.course.last_watch_at) }}
            </el-descriptions-item>
          </el-descriptions>

          <el-collapse class="progress-detail-chapters">
            <el-collapse-item
              v-for="chapter in detail.chapters"
              :key="chapter.chapter_id"
              :name="chapter.chapter_id"
              :title="chapter.title"
            >
              <div
                v-for="lesson in chapter.lessons"
                :key="lesson.lesson_id"
                class="progress-detail-lesson"
              >
                <span>{{ lesson.title }}</span>
                <span class="progress-detail-meta">
                  {{ formatPercent(lesson.percent) }} ·
                  {{ formatDuration(lesson.watch_seconds) }} ·
                  {{ formatRelative(lesson.last_watch_at) }}
                </span>
              </div>
            </el-collapse-item>
          </el-collapse>
          <el-empty v-if="detail.chapters.length === 0" description="尚未产生课时学习记录" />
        </template>
      </StateView>
    </el-drawer>
  </div>
</template>
