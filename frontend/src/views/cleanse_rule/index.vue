<template>
  <section class="page" data-module="cleanse_rule">
    <header class="page-head">
      <div>
        <h2>清洗策略管理</h2>
        <p class="page-desc">维护清洗策略的输入、执行约束与结果记录，为数据清洗提供可验证的数据工程流程。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" data-testid="open-create" type="button" @click="openCreate">登记清洗策略</button>
        <button class="btn" data-testid="export" type="button" @click="exportRows">导出清洗策略清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <form class="filter-bar" @submit.prevent="reload">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :data-testid="`filter-${field}`" :placeholder="`按${field}检索`" />
      </label>
      <button class="btn" data-testid="query" type="submit">查询</button>
      <button class="btn ghost" data-testid="reset" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)" :data-testid="`row-${row.id}`" :data-status="String(row.status)">
          <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
          <td class="row-actions">
            <button
              v-for="action in actions"
              :key="action"
              class="link"
              type="button"
              :data-testid="`action-${row.id}-${action}`"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 1" class="empty-state">暂无清洗策略数据，可先登记清洗策略</td>
        </tr>
      </tbody>
    </table>

    <div v-if="showCreate" class="modal-backdrop" data-testid="create-modal">
      <form class="modal" @submit.prevent="submitCreate">
        <h3>登记清洗策略</h3>
        <label v-for="field in columns" :key="field" class="create-field">
          <span>{{ field }}</span>
          <input v-model="createValues[field]" :data-testid="`create-${field}`" required />
        </label>
        <div class="page-actions">
          <button class="btn" type="button" @click="showCreate = false">取消</button>
          <button class="btn primary" data-testid="submit-create" type="submit" :disabled="creating">
            {{ creating ? '提交中…' : '确认登记' }}
          </button>
        </div>
      </form>
    </div>

    <div class="page-actions report-entry">
      <button class="btn" data-testid="open-report" type="button" @click="openReportPanel">
        清洗规则执行报告
      </button>
      <span v-if="reportMessage" class="error-text">{{ reportMessage }}</span>
    </div>

    <div v-if="showReport" class="modal-backdrop" data-testid="report-panel">
      <section class="modal report-modal">
        <header class="page-head">
          <div>
            <h3>清洗规则执行报告</h3>
            <p class="page-desc">按业务日期收拢跨月边界，复核规则编号、原始值、正则命中位置、填充值与替换原因。</p>
          </div>
          <button class="btn" type="button" @click="showReport = false">关闭</button>
        </header>

        <form class="filter-bar" @submit.prevent="loadReports">
          <label class="filter-item">
            <span>目标数据集</span>
            <input v-model="reportFilter.dataset" data-testid="report-filter-dataset" placeholder="如 ods_orders" />
          </label>
          <label class="filter-item">
            <span>业务月份</span>
            <input v-model="reportFilter.business_month" data-testid="report-filter-month" placeholder="yyyy-mm" />
          </label>
          <button class="btn" type="button" data-testid="report-query" @click="loadReports">查询报告</button>
        </form>

        <table class="data-table">
          <thead>
            <tr>
              <th>报告编号</th>
              <th>数据集</th>
              <th>业务日期</th>
              <th>业务月份</th>
              <th>状态</th>
              <th>处理进度</th>
              <th>命中数</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="report in reports" :key="report.run_id" :data-testid="`report-row-${report.run_id}`">
              <td>{{ report.run_id }}</td>
              <td>{{ report.dataset }}</td>
              <td>{{ report.business_date }}</td>
              <td>{{ report.business_month }}</td>
              <td>{{ report.status }}</td>
              <td>{{ report.processed_rows }}/{{ report.total_input_rows }}</td>
              <td>{{ totalHits(report) }}</td>
              <td class="row-actions">
                <button class="link" type="button" :data-testid="`report-detail-${report.run_id}`" @click="loadReportDetail(report.run_id)">
                  复核
                </button>
                <button
                  v-if="report.status === '中断'"
                  class="link"
                  type="button"
                  :data-testid="`report-resume-${report.run_id}`"
                  @click="resumeReport(report)"
                >
                  续写
                </button>
              </td>
            </tr>
            <tr v-if="!reports.length">
              <td colspan="8" class="empty-state">暂无清洗规则执行报告，可先在明细层构建或执行数据集清洗</td>
            </tr>
          </tbody>
        </table>

        <div v-if="activeReport" class="report-detail" data-testid="report-detail">
          <h4>报告 {{ activeReport.run_id }} · 规则命中明细</h4>
          <div v-for="record in activeReport.records" :key="record.rule_no" class="report-record">
            <p>
              规则 {{ record.rule_no }}（{{ record.rule_version }}）· 顺序 {{ record.exec_order }} ·
              字段 {{ record.target_field }} · 命中 {{ record.hit_count }}
              （空值填充 {{ record.null_fill_count }} / 正则替换 {{ record.regex_replace_count }} /
              正则合规 {{ record.regex_hit_count }}）
            </p>
            <p class="page-desc">跨月命中：{{ formatMonths(record.months) }}</p>
            <table class="data-table">
              <thead>
                <tr>
                  <th>业务日期</th>
                  <th>命中类型</th>
                  <th>原始值</th>
                  <th>正则命中位置</th>
                  <th>填充值</th>
                  <th>替换原因</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="(detail, idx) in record.hit_details" :key="`${record.rule_no}-${idx}`">
                  <td>{{ detail.business_date }}</td>
                  <td>{{ detail.hit_type }}</td>
                  <td>{{ detail.original_value }}</td>
                  <td>{{ detail.regex_hit_span ?? '—' }}</td>
                  <td>{{ detail.filled_value }}</td>
                  <td>{{ detail.reason }}</td>
                </tr>
              </tbody>
            </table>
          </div>

          <div v-if="activeReport.conflicts?.length" class="report-conflicts">
            <h4>版本冲突与变化字段清单（优先级待确认，原顺序与命中数已保留）</h4>
            <ul v-for="conflict in activeReport.conflicts" :key="`${conflict.historical_rule_no}-${conflict.current_rule_no}`">
              <li>
                顺序 {{ conflict.exec_order }}：{{ conflict.historical_rule_no }}({{ conflict.historical_version }})
                与 {{ conflict.current_rule_no }}({{ conflict.current_version }})
                <ul>
                  <li v-for="change in conflict.changed_fields" :key="change.field">
                    {{ change.field }}：{{ change.historical_value }} → {{ change.current_value }}
                    （具体程度 #{{ change.specificity_rank }}）
                  </li>
                </ul>
              </li>
            </ul>
          </div>

          <div v-if="activeReport.quality_writebacks?.length" class="report-quality">
            <h4>质量稽核回写</h4>
            <ul>
              <li v-for="wb in activeReport.quality_writebacks" :key="`${wb.quality_rule_no}-${wb.checked_at}`">
                {{ wb.quality_rule_no }}：通过 {{ wb.passed }} / 失败 {{ wb.failed }}
                （通过率 {{ (wb.pass_rate * 100).toFixed(2) }}%）
              </li>
            </ul>
          </div>
        </div>
      </section>
    </div>

    <footer class="page-foot">
      <span>共 {{ total }} 条清洗策略记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, string | number | null>

const ENDPOINT = '/api/cleanse_rule'
const columns = ["策略编号", "目标字段", "空值处理", "异常阈值", "正则表达式", "替换值", "策略版本", "执行顺序"]
const actions = ["提交校验", "标记生效", "停用策略"]
const statuses = ["草稿", "待校验", "已生效", "已停用"]
const metricNames = ["清洗策略总数", "待校验策略", "已生效策略"]

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = columns.slice(0, 3)
const showCreate = ref(false)
const creating = ref(false)
const createValues = ref<Record<string, string>>({})

// 清洗规则执行报告面板状态
type ReportRecord = Record<string, any>
const showReport = ref(false)
const reports = ref<ReportRecord[]>([])
const activeReport = ref<ReportRecord | null>(null)
const reportMessage = ref('')
const reportFilter = ref<Record<string, string>>({ dataset: '', business_month: '' })

const stats = computed(() => [
  { label: metricNames[0], value: total.value },
  { label: metricNames[1], value: rows.value.filter((row) => row.status === statuses[1]).length },
  { label: metricNames[2], value: rows.value.filter((row) => row.status === statuses[statuses.length - 1]).length },
])

function totalHits(report: ReportRecord): number {
  return (report.records ?? []).reduce((sum: number, rec: ReportRecord) => sum + Number(rec.hit_count || 0), 0)
}

function formatMonths(months: Record<string, number> | undefined): string {
  if (!months) return '—'
  return Object.entries(months).map(([month, count]) => `${month}: ${count}`).join(' / ')
}

async function openReportPanel() {
  showReport.value = true
  activeReport.value = null
  await loadReports()
}

async function loadReports() {
  reportMessage.value = ''
  const params = new URLSearchParams()
  if (reportFilter.value.dataset) params.set('dataset', reportFilter.value.dataset)
  if (reportFilter.value.business_month) params.set('business_month', reportFilter.value.business_month)
  try {
    const response = await request(`${ENDPOINT}/reports?${params.toString()}`)
    if (!response.ok) throw new Error('清洗规则执行报告读取失败')
    const payload = await response.json()
    reports.value = payload.items ?? []
  } catch (error) {
    reportMessage.value = error instanceof Error ? error.message : '清洗规则执行报告读取失败'
  }
}

async function loadReportDetail(runId: string) {
  reportMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/reports/${runId}`)
    if (!response.ok) throw new Error('清洗规则执行报告明细读取失败')
    activeReport.value = await response.json()
  } catch (error) {
    reportMessage.value = error instanceof Error ? error.message : '清洗规则执行报告明细读取失败'
  }
}

async function resumeReport(report: ReportRecord) {
  reportMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/reports/${report.run_id}/resume`, {
      method: 'POST',
      body: JSON.stringify({}),
    })
    if (!response.ok) {
      const payload = await response.json().catch(() => ({}))
      throw new Error(payload.detail || '续写失败')
    }
    await loadReports()
    await loadReportDetail(report.run_id)
  } catch (error) {
    reportMessage.value = error instanceof Error ? error.message : '清洗续写失败'
  }
}

function resetFilters() {
  filters.value = {}
  void reload()
}

async function exportRows() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value).toString()
  try {
    const response = await request(`${ENDPOINT}/export?${query}`)
    if (!response.ok) throw new Error('导出失败，请稍后重试')
    const url = URL.createObjectURL(await response.blob())
    const link = document.createElement('a')
    link.href = url
    link.download = 'cleanse_rule.csv'
    link.click()
    URL.revokeObjectURL(url)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '导出失败'
  }
}

function openCreate() {
  createValues.value = Object.fromEntries(columns.map((field) => [field, '']))
  errorMessage.value = ''
  showCreate.value = true
}

async function submitCreate() {
  creating.value = true
  errorMessage.value = ''
  try {
    const response = await request(ENDPOINT, {
      method: 'POST', body: JSON.stringify(createValues.value),
    })
    const payload = await response.json()
    if (!response.ok || !payload.ok) throw new Error(payload.message || '登记失败')
    showCreate.value = false
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '登记失败'
  } finally {
    creating.value = false
  }
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ action }),
    })
    if (!response.ok) {
      throw new Error('清洗策略动作未生效，请稍后重试')
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '清洗策略操作失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) {
      throw new Error('清洗策略列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '清洗策略列表读取失败'
  }
}

onMounted(reload)
</script>
