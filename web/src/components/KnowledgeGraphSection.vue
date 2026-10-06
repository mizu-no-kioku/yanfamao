<template>
  <div class="graph-section" v-if="isGraphSupported">
    <div class="graph-container-compact">
      <div v-if="!isGraphSupported" class="graph-disabled">
        <div class="disabled-content">
          <h4>知识图谱不可用</h4>
          <p>当前知识库类型 "{{ kbTypeLabel }}" 不支持知识图谱功能。</p>
          <p>只有研发猫类型的知识库支持知识图谱。</p>
        </div>
      </div>
      <div v-else class="graph-wrapper">
        <GraphCanvas
          ref="graphRef"
          :graph-data="graph.graphData"
          @node-click="handleGraphNodeClick"
          @edge-click="handleGraphEdgeClick"
          @canvas-click="graph.handleCanvasClick"
        >
          <template #top>
            <div class="compact-actions">
              <div class="actions-left graph-query-bar">
                <a-select
                  :value="queryParams.start ? queryParams.start.entity_id : undefined"
                  placeholder="起点（留空看全图）"
                  class="graph-entity-select"
                  show-search
                  allow-clear
                  :filter-option="false"
                  :loading="entitySearchLoading"
                  :options="
                    entityOptions.map((item) => ({
                      value: item.entity_id,
                      label: `${item.name} (${item.label})`
                    }))
                  "
                  @search="searchEntities"
                  @change="
                    (value, option) => {
                      queryParams.start = value
                        ? { entity_id: value, name: option && option.label }
                        : null
                    }
                  "
                />
                <a-select
                  :value="queryParams.end ? queryParams.end.entity_id : undefined"
                  placeholder="终点（可选）"
                  class="graph-entity-select"
                  show-search
                  allow-clear
                  :filter-option="false"
                  :loading="entitySearchLoading"
                  :options="
                    entityOptions.map((item) => ({
                      value: item.entity_id,
                      label: `${item.name} (${item.label})`
                    }))
                  "
                  @search="searchEntities"
                  @change="
                    (value, option) => {
                      queryParams.end = value
                        ? { entity_id: value, name: option && option.label }
                        : null
                    }
                  "
                />
                <a-input-number
                  v-model:value="queryParams.maxDepth"
                  :min="1"
                  :max="3"
                  :step="1"
                  class="graph-depth-input"
                />
                <a-button type="primary" :loading="graph.fetching" @click="loadGraph">
                  查询
                </a-button>
                <a-button
                  class="action-btn filters-action-btn"
                  :type="advancedPanelOpen ? 'primary' : 'default'"
                  @click="toggleAdvancedPanel"
                  title="高级筛选"
                  aria-label="高级筛选"
                >
                  <SlidersHorizontal :size="16" />
                </a-button>
                <a-button
                  class="action-btn settings-action-btn"
                  @click="toggleSettingsPanel"
                  title="图谱展示设置"
                  aria-label="图谱展示设置"
                >
                  <Settings :size="16" />
                </a-button>
              </div>
              <div v-if="isMilvus && !readonly" class="actions-right">
                <a-button
                  v-if="isMilvus && !readonly"
                  class="action-btn index-action-btn"
                  :class="{ 'has-index-label': hasPendingGraphChunks }"
                  @click="toggleBuildPanel"
                  :title="graphIndexButtonTitle"
                  :aria-label="graphIndexButtonTitle"
                >
                  <Database :size="16" />
                  <span v-if="hasPendingGraphChunks" class="index-status-label"
                    >{{ pendingGraphChunks }} 待索引</span
                  >
                  <span
                    v-if="graphIndexDotStatus"
                    class="status-dot"
                    :class="`status-dot--${graphIndexDotStatus}`"
                  ></span>
                </a-button>
              </div>
            </div>
          </template>
        </GraphCanvas>
        <ResourceEmptyState
          v-if="showGraphConfigEmpty"
          class="graph-empty-state"
          title="暂无知识图谱"
          :description="
            readonly
              ? '图谱尚未构建，请等待知识库管理员配置抽取器并索引后查看。'
              : '配置抽取器后，才能从当前知识库构建实体与关系。'
          "
          :icon="Network"
          full-height
        >
          <template v-if="!readonly" #actions>
            <a-button type="primary" class="lucide-icon-btn" @click="openGraphConfig">
              <Settings :size="16" />
              配置抽取器
            </a-button>
          </template>
        </ResourceEmptyState>
        <ResourceEmptyState
          v-else-if="showGraphDataEmpty"
          class="graph-empty-state"
          :title="graphDataEmptyTitle"
          :description="graphDataEmptyDescription"
          :icon="Network"
          full-height
        >
          <template #actions>
            <a-button
              v-if="!readonly && hasPendingGraphChunks && !isBuildActive"
              type="primary"
              class="lucide-icon-btn"
              @click="startGraphBuild"
            >
              <Database :size="16" />
              开始索引
            </a-button>
            <a-button v-else class="lucide-icon-btn" @click="loadGraph">
              <RefreshCw :size="16" :class="{ spin: graph.fetching }" />
              刷新图谱
            </a-button>
          </template>
        </ResourceEmptyState>

        <!-- 详情浮动卡片 -->
        <GraphDetailPanel
          :visible="graph.showDetailDrawer"
          :item="graph.selectedItem"
          :type="graph.selectedItemType"
          @close="graph.handleCanvasClick"
        />

        <!-- 设置浮动面板 -->
        <transition name="settings-expand">
          <div v-if="showSettings" class="floating-panel settings-panel">
            <div class="panel-header">
              <span class="panel-title">图谱展示设置</span>
            </div>
            <div class="panel-body">
              <a-form layout="vertical">
                <a-form-item label="最大节点数 (limit)">
                  <a-input-number
                    v-model:value="subgraphParams.maxNodes"
                    :min="10"
                    :max="1000"
                    :step="10"
                    style="width: 100%"
                  />
                </a-form-item>
                <div class="settings-actions">
                  <a-button @click="loadGraph" :disabled="graph.fetching">
                    <RefreshCw :size="14" :class="{ spin: graph.fetching }" />
                    刷新
                  </a-button>
                  <a-button type="primary" @click="applySettings">应用</a-button>
                </div>
              </a-form>
            </div>
          </div>
        </transition>

        <!-- 高级筛选浮动面板 -->
        <transition name="settings-expand">
          <div v-if="advancedPanelOpen" class="floating-panel advanced-panel">
            <div class="panel-header">
              <span class="panel-title">高级筛选</span>
            </div>
            <div class="panel-body">
              <div class="filter-group">
                <span class="filter-group-label">中间实体</span>
                <a-select
                  v-model:value="filters.intermediateEntities"
                  mode="multiple"
                  placeholder="类型 ∈ 集合"
                  class="filter-control"
                  :disabled="!hasEntityTypeOptions"
                  :options="filterOptions.entity_types.map((item) => ({ value: item, label: item }))"
                />
                <div v-if="!hasEntityTypeOptions" class="filter-group-hint">
                  该图谱还没有可用于筛选的实体类型。
                </div>
              </div>
              <div class="filter-group">
                <span class="filter-group-label">实体属性</span>
                <div v-if="!hasEntityAttributeOptions" class="filter-group-hint">
                  该图谱的实体还没有抽取到属性。
                </div>
                <div
                  v-for="(row, index) in filters.entityAttributes"
                  :key="`entity-attr-${index}`"
                  class="filter-row"
                >
                  <a-select
                    v-model:value="row.name"
                    placeholder="属性名"
                    class="filter-name"
                    :disabled="!hasEntityAttributeOptions"
                    :options="
                      entityAttributeNameOptions(row).map((item) => ({
                        value: item.name,
                        label: item.name
                      }))
                    "
                    @change="row.value = ''"
                  />
                  <span class="filter-eq">=</span>
                  <a-select
                    v-model:value="row.value"
                    placeholder="属性值"
                    class="filter-value"
                    show-search
                    :options="entityAttributeValueOptions(row.name)"
                  />
                  <a-button
                    type="text"
                    danger
                    class="action-btn"
                    aria-label="删除条件"
                    @click="removeEntityAttributeRow(index)"
                  >
                    <Trash2 :size="14" />
                  </a-button>
                  <a-button
                    v-if="entityAttributeNameOptions(row).length"
                    type="text"
                    class="action-btn"
                    aria-label="添加条件"
                    @click="addEntityAttributeRow"
                  >
                    <Plus :size="14" />
                  </a-button>
                </div>
              </div>
              <div class="filter-group">
                <span class="filter-group-label">实体间关系</span>
                <a-select
                  v-model:value="filters.relationTypes"
                  mode="multiple"
                  placeholder="类型 ∈ 集合"
                  class="filter-control"
                  :disabled="!hasRelationTypeOptions"
                  :options="filterOptions.relation_types.map((item) => ({ value: item, label: item }))"
                />
                <div v-if="!hasRelationTypeOptions" class="filter-group-hint">
                  该图谱还没有可用于筛选的关系类型。
                </div>
              </div>
              <div class="filter-group">
                <span class="filter-group-label">关系属性</span>
                <div v-if="!hasRelationAttributeOptions" class="filter-group-hint">
                  该图谱的关系还没有可筛选的属性。
                </div>
                <div
                  v-for="(row, index) in filters.relationAttributes"
                  :key="`relation-attr-${index}`"
                  class="filter-row"
                >
                  <a-select
                    v-model:value="row.name"
                    placeholder="属性名"
                    class="filter-name"
                    :disabled="!hasRelationAttributeOptions"
                    :options="
                      relationAttributeNameOptions(row).map((item) => ({
                        value: item.name,
                        label: item.name
                      }))
                    "
                    @change="row.value = ''"
                  />
                  <span class="filter-eq">=</span>
                  <a-auto-complete
                    v-model:value="row.value"
                    placeholder="属性值"
                    class="filter-value"
                    :options="relationAttributeValueOptions(row.name)"
                  />
                  <a-button
                    type="text"
                    danger
                    class="action-btn"
                    aria-label="删除条件"
                    @click="removeRelationAttributeRow(index)"
                  >
                    <Trash2 :size="14" />
                  </a-button>
                  <a-button
                    v-if="relationAttributeNameOptions(row).length"
                    type="text"
                    class="action-btn"
                    aria-label="添加条件"
                    @click="addRelationAttributeRow"
                  >
                    <Plus :size="14" />
                  </a-button>
                </div>
              </div>
              <div v-if="!graphAttributesReady" class="filter-warning">
                <span>该图谱尚未回填筛选属性，实体属性筛选暂不可用。</span>
                <a-button
                  v-if="!readonly"
                  size="small"
                  :loading="graphAttributesBackfilling"
                  :disabled="isBuildActive"
                  @click="runAttributeBackfill"
                >
                  立即回填
                </a-button>
              </div>
              <div class="settings-actions">
                <a-button
                  type="primary"
                  @click="
                    () => {
                      advancedPanelOpen = false
                      loadGraph()
                    }
                  "
                >
                  应用筛选
                </a-button>
              </div>
            </div>
          </div>
        </transition>

        <div v-if="graphTruncated" class="graph-hint">结果已按上限截断，可能不是全部匹配路径。</div>
        <div v-if="graphQueryError" class="graph-hint graph-hint--error">{{ graphQueryError }}</div>

        <!-- 索引管理浮动面板 -->
        <transition name="slide-fade">
          <div v-if="isMilvus && !readonly && showBuildPanel" class="floating-panel build-panel">
            <div class="panel-header">
              <span class="panel-title">索引管理</span>
              <a-button
                v-if="graphBuildStatus?.locked && !isBuildActive"
                size="small"
                type="text"
                title="修改配置"
                aria-label="修改配置"
                class="panel-config-btn"
                @click="openGraphConfig"
              >
                <Settings :size="14" />
              </a-button>
            </div>
            <div class="panel-body">
              <a-progress
                v-if="isBuildActive"
                :percent="graphBuildStatus?.build_task_progress ?? 0"
                :stroke-color="{ '0%': '#108ee9', '100%': '#87d068' }"
                size="small"
                style="margin-bottom: 10px"
              />
              <div class="stats-grid">
                <div class="stat-item">
                  <span class="stat-value">{{ graphBuildStatus?.total_chunks ?? '-' }}</span>
                  <span class="stat-label">总 Chunk</span>
                </div>
                <div class="stat-item">
                  <span class="stat-value">{{ graphBuildStatus?.pending_chunks ?? '-' }}</span>
                  <span class="stat-label">待构建</span>
                </div>
                <div class="stat-item">
                  <span class="stat-value">{{ graphBuildStatus?.indexed_chunks ?? '-' }}</span>
                  <span class="stat-label">已构建</span>
                </div>
                <div class="stat-item">
                  <span class="stat-value">{{ graphBuildStatus?.structured_chunks ?? '-' }}</span>
                  <span class="stat-label">结构完成</span>
                </div>
                <div
                  class="stat-item"
                  :class="{ 'is-clickable': extractionFailedCount > 0 }"
                  :role="extractionFailedCount > 0 ? 'button' : undefined"
                  :tabindex="extractionFailedCount > 0 ? 0 : undefined"
                  @click="openFailedChunkSamples"
                  @keydown.enter.prevent="openFailedChunkSamples"
                  @keydown.space.prevent="openFailedChunkSamples"
                >
                  <span class="stat-value">{{ extractionFailedCount }}</span>
                  <span class="stat-label">抽取失败</span>
                </div>
                <div class="stat-item">
                  <span class="stat-value">{{ vectorPendingCount }}</span>
                  <span class="stat-label">向量待处理</span>
                </div>
                <div class="stat-item">
                  <span class="stat-value">{{ vectorFailedCount }}</span>
                  <span class="stat-label">向量失败</span>
                </div>
                <div class="stat-item">
                  <span class="stat-value">{{ graphBuildStatus?.entity_count ?? '-' }}</span>
                  <span class="stat-label">实体</span>
                </div>
                <div class="stat-item">
                  <span class="stat-value">{{ graphBuildStatus?.relationship_count ?? '-' }}</span>
                  <span class="stat-label">关系</span>
                </div>
              </div>
              <div class="build-actions">
                <a-button
                  v-if="!graphBuildStatus?.locked"
                  type="primary"
                  block
                  @click="openGraphConfig"
                >
                  配置抽取器
                </a-button>
                <a-button v-else-if="isBuildActive" type="primary" block disabled>
                  构建中 {{ graphBuildStatus?.build_task_progress ?? 0 }}%
                </a-button>
                <a-button
                  v-else-if="isBuildFailed"
                  type="primary"
                  block
                  :disabled="!graphBuildStatus?.pending_chunks"
                  @click="vectorFailedCount ? retryGraphVectors() : startGraphBuild()"
                >
                  {{ vectorFailedCount ? '重试失败向量' : '重试索引' }}
                </a-button>
                <a-button
                  v-else
                  type="primary"
                  block
                  :disabled="!graphBuildStatus?.pending_chunks"
                  @click="startGraphBuild"
                >
                  开始索引
                </a-button>
              </div>
            </div>
          </div>
        </transition>
      </div>
    </div>

    <a-modal
      v-model:open="showGraphConfig"
      :title="graphConfigTitle"
      width="640px"
      @cancel="showGraphConfig = false"
    >
      <a-form layout="vertical">
        <div class="form-grid model-config-grid">
          <a-form-item label="模型">
            <ModelSelectorComponent
              :model_spec="graphConfigForm.model_spec"
              placeholder="选择抽取模型"
              @select-model="(spec) => (graphConfigForm.model_spec = spec)"
            />
          </a-form-item>
          <a-form-item label="LLM 抽取并发数">
            <a-input-number
              v-model:value="graphConfigForm.concurrency_count"
              :min="1"
              :max="1000"
              :step="1"
              style="width: 100%"
            />
          </a-form-item>
        </div>
        <a-form-item label="Schema">
          <div v-if="isEditingGraphConfig" class="schema-description">
            修改配置仅影响后续构建；已构建的图谱不会自动重算，如需一致请重置后重新抽取。抽取器类型创建后不可修改。
          </div>
          <a-textarea
            v-model:value="graphConfigForm.schema"
            :rows="6"
            placeholder="描述实体类型、关系类型和属性约束。后端会把 Schema 拼接到固定抽取 Prompt 中。"
          />
        </a-form-item>
        <a-form-item label="单次抽取超时（秒）">
          <a-input-number
            v-model:value="graphConfigForm.timeout_seconds"
            :min="1"
            :max="600"
            :step="10"
            style="width: 100%"
          />
          <div class="form-item-hint">
            默认 60 秒。推理型抽取模型单块耗时可达数十秒，若日志出现反复超时重试，可调大至 180–300。
          </div>
        </a-form-item>
        <a-form-item label="模型参数 JSON">
          <a-input
            v-model:value="graphConfigForm.model_params_text"
            placeholder='例如 {"temperature":0.1}'
          />
          <div class="form-item-hint">
            输入的 JSON 对象会作为 model_params 传给抽取模型调用；如需设置超时，请使用上方字段。关闭百炼等模型的思考模式需写在
            extra_body 中，例如 {"extra_body":{"enable_thinking":false}}。
          </div>
        </a-form-item>
      </a-form>
      <template #footer>
        <div class="graph-config-footer">
          <a-button v-if="isEditingGraphConfig" danger @click="confirmResetGraph">重置</a-button>
          <div class="graph-config-footer-actions">
            <a-button @click="showGraphConfig = false">取消</a-button>
            <a-button type="primary" @click="configureGraphBuild">确认</a-button>
          </div>
        </div>
      </template>
    </a-modal>

    <a-modal v-model:open="showFailedChunkSamples" title="样例 Chunk" width="760px" :footer="null">
      <div v-if="failedChunkSamplesLoading" class="failed-chunk-loading">
        <Loader2 :size="18" class="spin" />
        正在加载失败 Chunk
      </div>
      <a-empty v-else-if="!failedChunkSamples.length" description="暂无抽取失败的 Chunk" />
      <a-tabs v-else v-model:activeKey="activeFailedChunkKey" class="failed-chunk-tabs">
        <a-tab-pane
          v-for="(chunk, index) in failedChunkSamples"
          :key="chunk.chunk_id"
          :tab="`样例 ${index + 1}`"
        >
          <div class="failed-chunk-meta">
            <span>Chunk {{ Number(chunk.chunk_index) + 1 }}</span>
            <span>{{ chunk.file_id }}</span>
            <span>尝试 {{ chunk.details?.attempt_count ?? '-' }} 次</span>
          </div>
          <a-alert
            type="error"
            :message="chunk.details?.last_error || '未记录失败原因'"
            show-icon
          />
          <pre class="failed-chunk-content">{{ chunk.content }}</pre>
        </a-tab-pane>
      </a-tabs>
    </a-modal>
  </div>
</template>

<script setup>
import { ref, computed, watch, nextTick, onUnmounted, reactive } from 'vue'
import { useDatabaseStore } from '@/stores/database'
import { useTaskerStore } from '@/stores/tasker'
import { useConfigStore } from '@/stores/config'
import {
  RefreshCw,
  Settings,
  Loader2,
  Database,
  Network,
  SlidersHorizontal,
  Plus,
  Trash2
} from '@lucide/vue'
import GraphCanvas from '@/components/GraphCanvas.vue'
import GraphDetailPanel from '@/components/GraphDetailPanel.vue'
import ResourceEmptyState from '@/components/shared/ResourceEmptyState.vue'
import { getKbTypeLabel } from '@/utils/kb_utils'
import { unifiedApi } from '@/apis/graph_api'
import { graphBuildApi } from '@/apis/knowledge_api'
import { Modal, message } from 'ant-design-vue'
import ModelSelectorComponent from '@/components/ModelSelectorComponent.vue'
import { useGraph } from '@/composables/useGraph'
import { availableAttributeNames, buildSubgraphPayload } from '@/utils/graph_query_payload'

const GRAPH_BUILD_TASK_TYPE = 'knowledge_graph_index'
const MILVUS_KB_TYPE = 'milvus'
const GRAPH_SUPPORTED_KB_TYPES = new Set([MILVUS_KB_TYPE])

const props = defineProps({
  active: {
    type: Boolean,
    default: false
  },
  readonly: {
    type: Boolean,
    default: false
  }
})

const store = useDatabaseStore()
const taskerStore = useTaskerStore()
const configStore = useConfigStore()

const kbId = computed(() => store.kbId)
const kbType = computed(() => store.database.kb_type)
const kbTypeLabel = computed(() => getKbTypeLabel(kbType.value || 'milvus'))
const isMilvus = computed(() => kbType.value?.toLowerCase() === MILVUS_KB_TYPE)

const graphRef = ref(null)
const showSettings = ref(false)
const showBuildPanel = ref(false)
// 起点/终点取代了原来的关键词搜索框；层数移到工具栏，Chunk 不再进入路径，因此不再有排除开关。
const subgraphParams = reactive({
  maxNodes: 100
})
const queryParams = reactive({
  start: null,
  end: null,
  maxDepth: 2
})
const advancedPanelOpen = ref(false)
const graphAttributesReady = ref(true)
const graphAttributesBackfilling = ref(false)
const graphTruncated = ref(false)
const graphQueryError = ref('')
const entityOptions = ref([])
const entitySearchLoading = ref(false)
const filterOptions = ref({
  entity_types: [],
  entity_attributes: [],
  relation_types: [],
  relation_attributes: []
})
const filters = reactive({
  intermediateEntities: [],
  entityAttributes: [{ name: '', value: '' }],
  relationTypes: [],
  relationAttributes: [{ name: 'text', value: '' }]
})

const entityAttributeNameOptions = (row) =>
  availableAttributeNames(filterOptions.value.entity_attributes, filters.entityAttributes, row)
const relationAttributeNameOptions = (row) =>
  availableAttributeNames(filterOptions.value.relation_attributes, filters.relationAttributes, row)

// 某个分组没有可取的值时，把控件禁用并说明原因：否则用户会往里输入、失焦后被清空，
// 看起来像「输入丢了」，实际是这个图谱里没有这一类数据。
const hasEntityTypeOptions = computed(() => filterOptions.value.entity_types.length > 0)
const hasRelationTypeOptions = computed(() => filterOptions.value.relation_types.length > 0)
const hasEntityAttributeOptions = computed(() => filterOptions.value.entity_attributes.length > 0)
const hasRelationAttributeOptions = computed(() => filterOptions.value.relation_attributes.length > 0)
const entityAttributeValueOptions = (name) =>
  (filterOptions.value.entity_attributes.find((item) => item.name === name)?.values || []).map(
    (item) => ({ value: item, label: item })
  )
const relationAttributeValueOptions = (name) =>
  (filterOptions.value.relation_attributes.find((item) => item.name === name)?.values || []).map(
    (item) => ({ value: item })
  )
const addEntityAttributeRow = () => filters.entityAttributes.push({ name: '', value: '' })
const removeEntityAttributeRow = (index) => {
  if (filters.entityAttributes.length === 1) {
    filters.entityAttributes[0].name = ''
    filters.entityAttributes[0].value = ''
    return
  }
  filters.entityAttributes.splice(index, 1)
}
const addRelationAttributeRow = () => filters.relationAttributes.push({ name: '', value: '' })
const removeRelationAttributeRow = (index) => {
  if (filters.relationAttributes.length === 1) {
    filters.relationAttributes[0].name = ''
    filters.relationAttributes[0].value = ''
    return
  }
  filters.relationAttributes.splice(index, 1)
}

/** 请求体由纯函数装配，这里只负责把面板状态喂给它。 */
const currentSubgraphPayload = () =>
  buildSubgraphPayload({
    kbId: kbId.value,
    start: queryParams.start,
    end: queryParams.end,
    maxDepth: queryParams.maxDepth,
    maxNodes: subgraphParams.maxNodes,
    filters
  })

const loadFilterOptions = async () => {
  if (!kbId.value || !isGraphSupported.value) return
  try {
    const res = await unifiedApi.getFilterOptions(kbId.value)
    if (res.success && res.data) {
      filterOptions.value = res.data
      graphAttributesReady.value = Boolean(res.data.attributes_ready)
    }
  } catch (e) {
    console.error('Failed to load filter options:', e)
  }
}

const searchEntities = async (keyword) => {
  entitySearchLoading.value = true
  try {
    const res = await unifiedApi.searchEntities({ kbId: kbId.value, q: keyword })
    entityOptions.value = res.success && res.data ? res.data.entities : []
  } catch (e) {
    console.error('Failed to search entities:', e)
    entityOptions.value = []
  } finally {
    entitySearchLoading.value = false
  }
}
const graphBuildStatus = ref(null)
const graphBuildLoading = ref(false)
const showGraphConfig = ref(false)
const showFailedChunkSamples = ref(false)
const failedChunkSamplesLoading = ref(false)
const failedChunkSamples = ref([])
const activeFailedChunkKey = ref('')
let buildStatusPollTimer = null

const isBuildActive = computed(() => {
  const s = graphBuildStatus.value?.build_task_status
  return s === 'pending' || s === 'running'
})

const isBuildFailed = computed(() => {
  return graphBuildStatus.value?.build_task_status === 'failed'
})

const pendingGraphChunks = computed(() => {
  return Number(graphBuildStatus.value?.pending_chunks ?? 0)
})

const hasPendingGraphChunks = computed(() => pendingGraphChunks.value > 0)
const extractionFailedCount = computed(() =>
  Number(graphBuildStatus.value?.extraction_counts?.failed || 0)
)
const vectorPendingCount = computed(() => {
  const counts = graphBuildStatus.value?.vector_counts || {}
  return Number(counts.pending || 0) + Number(counts.processing || 0)
})
const vectorFailedCount = computed(() => Number(graphBuildStatus.value?.vector_counts?.failed || 0))

const isGraphIndexComplete = computed(() => {
  return (
    Boolean(graphBuildStatus.value?.locked) &&
    !isBuildActive.value &&
    pendingGraphChunks.value === 0
  )
})

const graphIndexDotStatus = computed(() => {
  if (isBuildActive.value) return 'active'
  if (hasPendingGraphChunks.value) return 'pending'
  if (isGraphIndexComplete.value) return 'complete'
  return ''
})

const graphIndexButtonTitle = computed(() => {
  if (hasPendingGraphChunks.value) return `索引管理，${pendingGraphChunks.value} 待索引`
  if (isGraphIndexComplete.value) return '索引管理，已全部索引'
  if (isBuildActive.value) return '索引管理，索引中'
  return '索引管理'
})

/**
 * 收起所有浮动面板。
 *
 * 节点详情、展示设置、高级筛选共用同一定位与层级（top:60px / left:10px / z-index:100），
 * 详情卡片在 DOM 里更靠前，所以后展开的面板会盖住它。三者必须互斥，打开任何一个前先全部收起；
 * 构建面板虽然靠右，也一并收起，避免同时堆叠多个浮层。
 */
const closeDockedPanels = () => {
  showSettings.value = false
  showBuildPanel.value = false
  advancedPanelOpen.value = false
}

const toggleBuildPanel = () => {
  const next = !showBuildPanel.value
  closeDockedPanels()
  showBuildPanel.value = next
  if (next) graph.handleCanvasClick()
}

const toggleSettingsPanel = () => {
  const next = !showSettings.value
  closeDockedPanels()
  showSettings.value = next
  if (next) graph.handleCanvasClick()
}

const toggleAdvancedPanel = () => {
  const next = !advancedPanelOpen.value
  closeDockedPanels()
  advancedPanelOpen.value = next
  if (next) graph.handleCanvasClick()
}

const handleGraphNodeClick = (nodeData) => {
  // 详情卡片要和这些面板争同一个位置：点击节点时必须先收起它们，否则详情被盖住。
  closeDockedPanels()
  graph.handleNodeClick(nodeData)
}

const handleGraphEdgeClick = (edgeData) => {
  closeDockedPanels()
  graph.handleEdgeClick(edgeData)
}

const isEditingGraphConfig = computed(() => Boolean(graphBuildStatus.value?.locked))

const graphConfigTitle = computed(() =>
  isEditingGraphConfig.value ? '修改图谱抽取配置' : '图谱展示配置'
)

const stopBuildStatusPoll = () => {
  if (buildStatusPollTimer) {
    clearInterval(buildStatusPollTimer)
    buildStatusPollTimer = null
  }
}

const startBuildStatusPoll = () => {
  stopBuildStatusPoll()
  buildStatusPollTimer = setInterval(() => {
    loadGraphBuildStatus()
  }, 5000)
}

let hadActiveBuildTask = false
watch(
  isBuildActive,
  (active) => {
    if (active) {
      startBuildStatusPoll()
      hadActiveBuildTask = true
      return
    }
    stopBuildStatusPoll()
    if (hadActiveBuildTask) {
      // 构建 / 回填 / 向量修复结束后图谱内容或属性投影可能已变化，重新取一次筛选选项；
      // 只有「从活动变为不活动」才刷新，避免 immediate 在挂载时多发一次请求。
      hadActiveBuildTask = false
      loadFilterOptions()
    }
  },
  { immediate: true }
)
const DEFAULT_EXTRACTION_TIMEOUT_SECONDS = 60
const graphConfigForm = reactive({
  model_spec: '',
  schema: '',
  concurrency_count: 50,
  timeout_seconds: DEFAULT_EXTRACTION_TIMEOUT_SECONDS,
  model_params_text: ''
})

const graph = reactive(useGraph(graphRef))
const graphLoaded = ref(false)

// 计算属性：是否支持知识图谱
const isGraphSupported = computed(() => GRAPH_SUPPORTED_KB_TYPES.has(kbType.value?.toLowerCase()))
const hasGraphNodes = computed(() => graph.graphData.nodes.length > 0)
const showGraphConfigEmpty = computed(
  () => isMilvus.value && !graphBuildStatus.value?.locked && !graphBuildLoading.value
)
const showGraphDataEmpty = computed(
  () =>
    isMilvus.value &&
    Boolean(graphBuildStatus.value?.locked) &&
    graphLoaded.value &&
    !graph.fetching &&
    !hasGraphNodes.value
)
const graphDataEmptyTitle = computed(() => (queryParams.start ? '未找到匹配路径' : '暂无知识图谱'))
const graphDataEmptyDescription = computed(() => {
  if (isBuildActive.value) return '图谱索引正在运行，完成后会展示实体与关系。'
  if (hasPendingGraphChunks.value) return '当前还有待索引 Chunk，完成索引后会展示实体与关系。'
  if (queryParams.start) return '换个起点、增加层数或放宽筛选条件后再查询。'
  return '当前知识库还没有可展示的实体与关系。'
})

let pendingLoadTimer = null
let graphStatusRequestSeq = 0
let graphLoadRequestSeq = 0

const getErrorDetail = (e, fallback) => {
  return e?.response?.data?.detail || e?.response?.data?.message || e?.message || fallback
}

const loadGraphBuildStatus = async () => {
  if (!kbId.value || !isMilvus.value) return
  const requestSeq = ++graphStatusRequestSeq
  const currentDatabaseId = kbId.value
  graphBuildLoading.value = true
  try {
    const status = await graphBuildApi.getStatus(currentDatabaseId)
    if (requestSeq === graphStatusRequestSeq && currentDatabaseId === kbId.value) {
      graphBuildStatus.value = status
    }
  } catch (e) {
    console.error('Failed to load graph build status:', e)
    message.error('加载图谱构建状态失败')
  } finally {
    if (requestSeq === graphStatusRequestSeq) {
      graphBuildLoading.value = false
    }
  }
}

const parseModelParams = () => {
  const text = graphConfigForm.model_params_text.trim()
  if (!text) return {}
  let params
  try {
    params = JSON.parse(text)
  } catch {
    throw new Error('模型参数必须是合法 JSON 对象')
  }
  if (!params || Array.isArray(params) || typeof params !== 'object') {
    throw new Error('模型参数必须是 JSON 对象')
  }
  return params
}

const fillGraphConfigForm = () => {
  const config = graphBuildStatus.value?.config
  const options = config?.extractor_options || {}
  graphConfigForm.model_spec = options.model_spec || configStore.config?.default_model || ''
  graphConfigForm.schema = options.schema || ''
  graphConfigForm.concurrency_count = Number(options.concurrency_count || 50)
  graphConfigForm.timeout_seconds = Number(options.timeout_seconds || DEFAULT_EXTRACTION_TIMEOUT_SECONDS)
  graphConfigForm.model_params_text = options.model_params
    ? JSON.stringify(options.model_params)
    : ''
}

const openGraphConfig = () => {
  fillGraphConfigForm()
  showGraphConfig.value = true
}

const buildExtractorOptions = () => {
  return {
    model_spec: graphConfigForm.model_spec,
    schema: graphConfigForm.schema.trim(),
    concurrency_count: graphConfigForm.concurrency_count || 50,
    timeout_seconds: graphConfigForm.timeout_seconds || DEFAULT_EXTRACTION_TIMEOUT_SECONDS,
    model_params: parseModelParams()
  }
}

const configureGraphBuild = async () => {
  try {
    document.activeElement?.blur()
    await nextTick()
    await graphBuildApi.configure(kbId.value, {
      extractor_type: 'llm',
      extractor_options: buildExtractorOptions()
    })
    message.success(isEditingGraphConfig.value ? '图谱抽取配置已更新' : '图谱抽取配置已保存')
    showGraphConfig.value = false
    await loadGraphBuildStatus()
  } catch (e) {
    console.error('Failed to configure graph build:', e)
    message.error(getErrorDetail(e, '配置图谱抽取失败'))
  }
}

const startGraphBuild = async () => {
  const registerTask = taskerStore.createTaskRegistration()
  try {
    const data = await graphBuildApi.startIndex(kbId.value)
    message.success(data.message || '图谱构建任务已提交')
    if (data.task_id) {
      registerTask({
        task_id: data.task_id,
        name: `图谱构建 (${kbId.value})`,
        task_type: GRAPH_BUILD_TASK_TYPE,
        message: data.message,
        payload: { kb_id: kbId.value }
      })
    }
    await loadGraphBuildStatus()
  } catch (e) {
    console.error('Failed to start graph build:', e)
    message.error(getErrorDetail(e, '提交图谱构建任务失败'))
  }
}

const retryGraphVectors = async () => {
  const registerTask = taskerStore.createTaskRegistration()
  try {
    const data = await graphBuildApi.reconcile(kbId.value, 'failed')
    message.success(data.message || '图谱向量索引修复任务已提交')
    if (data.task_id) {
      registerTask({
        task_id: data.task_id,
        name: `图谱向量索引修复 (${kbId.value})`,
        task_type: GRAPH_BUILD_TASK_TYPE,
        message: data.message,
        payload: { kb_id: kbId.value, reconcile_mode: 'failed' }
      })
    }
    await loadGraphBuildStatus()
  } catch (e) {
    console.error('Failed to reconcile graph vectors:', e)
    message.error(getErrorDetail(e, '提交图谱向量索引修复任务失败'))
  }
}

/**
 * 提交「筛选属性回填」任务。
 *
 * 升级前建好的图谱没有新的属性投影，按实体属性筛选会得到空结果；后端用就绪门拦住并提示先回填，
 * 这里提供解除这个状态的入口——没有它，用户会卡在一个无法执行下去的提示上。
 */
const runAttributeBackfill = async () => {
  const registerTask = taskerStore.createTaskRegistration()
  graphAttributesBackfilling.value = true
  try {
    const data = await graphBuildApi.backfillAttributes(kbId.value)
    message.success(data.message || '图谱筛选属性回填任务已提交')
    if (data.task_id) {
      registerTask({
        task_id: data.task_id,
        name: `图谱筛选属性回填 (${kbId.value})`,
        task_type: GRAPH_BUILD_TASK_TYPE,
        message: data.message,
        payload: { kb_id: kbId.value, action: 'backfill_attributes' }
      })
    }
    await loadGraphBuildStatus()
  } catch (e) {
    console.error('Failed to backfill graph attributes:', e)
    message.error(getErrorDetail(e, '提交图谱筛选属性回填任务失败'))
  } finally {
    graphAttributesBackfilling.value = false
  }
}

const openFailedChunkSamples = async () => {
  if (!extractionFailedCount.value || failedChunkSamplesLoading.value) return
  showFailedChunkSamples.value = true
  failedChunkSamplesLoading.value = true
  failedChunkSamples.value = []
  activeFailedChunkKey.value = ''
  try {
    const data = await graphBuildApi.getFailedChunks(kbId.value, 10)
    failedChunkSamples.value = data.samples || []
    activeFailedChunkKey.value = failedChunkSamples.value[0]?.chunk_id || ''
  } catch (e) {
    console.error('Failed to load graph extraction failed chunks:', e)
    message.error(getErrorDetail(e, '加载抽取失败 Chunk 失败'))
  } finally {
    failedChunkSamplesLoading.value = false
  }
}

const confirmResetGraph = () => {
  Modal.confirm({
    title: '清空并重建图谱',
    content: '将删除该知识库在 Neo4j 中的图谱，重置 Chunk 图谱状态，并清空抽取结果与配置。',
    okText: '确认重置',
    cancelText: '取消',
    onOk: resetGraphBuild
  })
}

const resetGraphBuild = async () => {
  try {
    await graphBuildApi.reset(kbId.value, {
      clear_extraction_result: true,
      clear_config: true
    })
    message.success('图谱构建状态已重置')
    showGraphConfig.value = false
    graphLoaded.value = false
    graph.clearGraph()
    await loadGraphBuildStatus()
  } catch (e) {
    console.error('Failed to reset graph build:', e)
    message.error(getErrorDetail(e, '重置图谱构建状态失败'))
  }
}

const loadGraph = async () => {
  if (!kbId.value || !isGraphSupported.value) return

  const requestSeq = ++graphLoadRequestSeq
  const currentDatabaseId = kbId.value
  graph.fetching = true
  graphQueryError.value = ''
  graphTruncated.value = false
  if (!hasGraphNodes.value) {
    graphLoaded.value = false
  }
  try {
    const res = await unifiedApi.querySubgraph(currentSubgraphPayload())

    if (
      requestSeq === graphLoadRequestSeq &&
      currentDatabaseId === kbId.value &&
      res.success &&
      res.data
    ) {
      graphTruncated.value = Boolean(res.data.truncated)
      graph.updateGraphData(res.data.nodes, res.data.edges)
    }
  } catch (e) {
    graphQueryError.value =
      e.status === 409 ? '该图谱尚未回填筛选属性，请先执行属性回填' : e.message || '加载图谱失败'
    if (e.status !== 409) message.error(graphQueryError.value)
  } finally {
    if (requestSeq === graphLoadRequestSeq) {
      graph.fetching = false
      graphLoaded.value = true
    }
  }
}

const applySettings = () => {
  showSettings.value = false
  loadGraph()
}

const scheduleGraphLoad = (delay = 200) => {
  if (!props.active || !isGraphSupported.value || !kbId.value) {
    return
  }

  if (pendingLoadTimer) {
    clearTimeout(pendingLoadTimer)
  }
  pendingLoadTimer = setTimeout(async () => {
    pendingLoadTimer = null
    await nextTick()
    if (props.active && isGraphSupported.value && kbId.value) {
      await loadGraph()
    }
  }, delay)
}

watch(
  () => props.active,
  (active) => {
    if (active) {
      if (isMilvus.value) {
        loadGraphBuildStatus()
      }
      // 这个 watcher 是首次挂载时唯一会跑的（immediate），筛选选项必须在这里加载；
      // watch(kbId) / watch(isGraphSupported) 都没有 immediate，换页时不一定触发。
      loadFilterOptions()
      // 起点/终点的选项原本只有输入过才有，点开是空的；先拉一批默认实体填进去。
      searchEntities('')
      scheduleGraphLoad()
    }
  },
  { immediate: true }
)

watch(kbId, () => {
  graphStatusRequestSeq += 1
  graphLoadRequestSeq += 1
  graphLoaded.value = false
  graph.clearGraph()
  graphBuildStatus.value = null
  showFailedChunkSamples.value = false
  failedChunkSamples.value = []
  if (isMilvus.value) {
    loadGraphBuildStatus()
  }
  if (isGraphSupported.value) {
    loadFilterOptions()
    scheduleGraphLoad(300)
  }
})

watch(isGraphSupported, (supported) => {
  if (!supported) {
    graphLoaded.value = false
    graph.clearGraph()
    graphBuildStatus.value = null
    return
  }
  if (isMilvus.value) {
    loadGraphBuildStatus()
  }
  loadFilterOptions()
  scheduleGraphLoad(200)
})

onUnmounted(() => {
  if (pendingLoadTimer) {
    clearTimeout(pendingLoadTimer)
    pendingLoadTimer = null
  }
  stopBuildStatusPoll()
})
</script>

<style scoped lang="less">
.graph-section {
  height: 100%;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  position: relative;
  user-select: none;
}

.graph-container-compact {
  flex: 1;
  min-height: 0;
  overflow: hidden;
  position: relative;
}

.graph-wrapper {
  height: 100%;
  width: 100%;
  position: relative;
  --graph-search-input-width: 240px;
  --graph-search-toolbar-width: calc(var(--graph-search-input-width) + 78px);
}

.graph-empty-state {
  position: absolute;
  inset: 0;
  z-index: 30;
  pointer-events: none;

  :deep(.resource-empty-state__actions) {
    pointer-events: auto;
  }
}

.compact-actions {
  position: absolute;
  top: 10px;
  left: 10px;
  right: 10px;
  display: flex;
  justify-content: space-between;
  align-items: center;
  pointer-events: none; /* Let clicks pass through empty areas */

  .actions-left,
  .actions-right {
    pointer-events: auto; /* Re-enable clicks for buttons/inputs */
    display: flex;
    align-items: center;
    gap: 0;
    background: var(--color-trans-light);
    backdrop-filter: blur(12px);
    padding: 2px;
    border-radius: 8px;
    box-shadow: 0 0 4px 0px var(--shadow-2);
    border: 1px solid var(--gray-100);
  }

  :deep(.ant-input-affix-wrapper) {
    padding: 4px 11px;
    border-radius: 6px;
    border-color: transparent;
    box-shadow: none;
    background: var(--color-trans-light);

    &:hover,
    &:focus,
    &-focused {
      background: var(--main-0);
      border-color: var(--primary-color);
    }

    input {
      background: transparent;
    }
  }

  .graph-search-input {
    flex: 0 0 var(--graph-search-input-width);
    width: var(--graph-search-input-width);
    border-radius: 0;
  }

  .search-action-btn {
    border-radius: 6px 0 0 6px;
  }

  .settings-action-btn {
    border-radius: 0 6px 6px 0;
  }

  .action-btn {
    width: 32px;
    height: 32px;
    padding: 0;
    display: flex;
    align-items: center;
    justify-content: center;
    border: none;
    background: transparent;
    color: var(--gray-600);
    border-radius: 6px;
    box-shadow: none;
    position: relative;

    &:hover {
      background: var(--shadow-1);
      color: var(--primary-color);
    }
  }

  .index-action-btn {
    gap: 6px;
    overflow: visible;

    &.has-index-label {
      width: auto;
      min-width: 84px;
      padding: 0 22px 0 8px;
      justify-content: flex-start;
    }

    .index-status-label {
      font-size: 12px;
      line-height: 1;
      color: var(--gray-700);
      white-space: nowrap;
    }
  }

  .status-dot {
    position: absolute;
    bottom: 4px;
    right: 4px;
    width: 7px;
    height: 7px;
    border-radius: 50%;
    box-shadow: 0 0 0 1px var(--color-trans-light);
  }

  .status-dot--pending {
    background: var(--color-warning-500);
  }

  .status-dot--active {
    background: var(--color-warning-500);
    animation: blink 1.2s ease-in-out infinite;
  }

  .status-dot--complete {
    background: var(--color-success-500);
  }

  .spin {
    animation: spin 1s linear infinite;
  }
}

@keyframes blink {
  0%,
  100% {
    opacity: 1;
  }
  50% {
    opacity: 0.2;
  }
}

.graph-disabled {
  display: flex;
  justify-content: center;
  align-items: center;
  height: 100%;
}

.disabled-content {
  text-align: center;
  color: var(--gray-400);

  h4 {
    margin-bottom: 8px;
  }
}

.floating-panel {
  position: absolute;
  top: 60px;
  right: 10px;
  width: 300px;
  max-height: calc(100% - 60px);
  overflow-y: auto;
  z-index: 100;
  background: var(--color-trans-light);
  backdrop-filter: blur(12px);
  -webkit-backdrop-filter: blur(12px);
  border-radius: 8px;
  border: 1px solid var(--gray-100);
  box-shadow: 0 0 4px 0px var(--shadow-2);
  font-size: 13px;

  .panel-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 10px 14px;
    border-bottom: 1px solid var(--gray-200);

    .panel-title {
      font-size: 13px;
      font-weight: 600;
      color: var(--gray-1000);
    }

    .panel-config-btn {
      padding: 2px 6px;
    }
  }

  .panel-body {
    padding: 10px 14px;
  }
}

.settings-panel {
  left: 10px;
  right: auto;
  width: min(var(--graph-search-toolbar-width), calc(100% - 20px));

  .panel-body {
    :deep(.ant-form-item) {
      margin-bottom: 12px;
    }
  }

  .settings-exclude-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    min-height: 32px;
    margin: 4px 0 14px;
    color: var(--gray-800);
  }

  .settings-field-label {
    font-size: 13px;
  }

  .settings-actions {
    display: flex;
    align-items: center;
    justify-content: flex-end;
    gap: 8px;

    :deep(.ant-btn) {
      display: inline-flex;
      align-items: center;
      gap: 6px;
    }
  }
}

.compact-actions {
  // 覆盖 .actions-left 的 gap: 0，让查询栏的控件之间留出间隔。
  .graph-query-bar {
    gap: 6px;
  }

  .graph-entity-select {
    width: 200px;
  }

  .graph-depth-input {
    width: 96px;
  }
}

.advanced-panel {
  left: 10px;
  right: auto;
  width: min(520px, calc(100% - 20px));

  .filter-group {
    margin-bottom: 12px;
  }

  .filter-group-label {
    display: block;
    margin-bottom: 4px;
    font-size: 13px;
    color: var(--gray-800);
  }

  .filter-row {
    display: flex;
    align-items: center;
    gap: 6px;
    margin-bottom: 6px;
  }

  .filter-control {
    width: 100%;
  }

  .filter-name {
    flex: 0 0 38%;
  }

  .filter-value {
    flex: 1;
  }

  .filter-eq {
    color: var(--gray-600);
  }

  .filter-warning {
    display: flex;
    align-items: center;
    gap: 8px;
    margin: 4px 0 12px;
    font-size: 12px;
    color: var(--color-warning-900);
  }

  .filter-group-hint {
    margin-top: 4px;
    font-size: 12px;
    color: var(--gray-600);
  }
}

.graph-hint {
  position: absolute;
  bottom: 10px;
  left: 10px;
  z-index: 90;
  padding: 4px 10px;
  border-radius: 6px;
  border: 1px solid var(--gray-100);
  background: var(--color-trans-light);
  color: var(--gray-800);
  font-size: 12px;

  &--error {
    color: var(--color-error-700);
  }
}

.build-panel {
  .stats-grid {
    display: grid;
    grid-template-columns: repeat(3, 1fr);
    gap: 8px;
    margin-bottom: 12px;
  }

  .stat-item {
    display: flex;
    flex-direction: column;
    align-items: center;
    padding: 6px 4px;
    border-radius: 4px;
    background: var(--gray-0);
    outline: 1px solid var(--gray-100);

    &.is-clickable {
      cursor: pointer;
      border: 1px solid var(--color-error-100);

      &:hover,
      &:focus-visible {
        background: var(--color-error-50);
        outline: none;
      }

      .stat-value,
      .stat-label {
        color: var(--color-error-700);
      }
    }

    .stat-value {
      font-size: 15px;
      font-weight: 600;
      color: var(--gray-1000);
      line-height: 1.2;
    }

    .stat-label {
      font-size: 11px;
      color: var(--gray-500);
      margin-top: 2px;
    }
  }

  .build-actions {
    display: flex;
    flex-direction: column;
    gap: 8px;
  }
}

.failed-chunk-loading {
  min-height: 180px;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  color: var(--gray-600);
}

.failed-chunk-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 8px 16px;
  margin-bottom: 12px;
  color: var(--gray-600);
  font-size: 12px;
}

.failed-chunk-content {
  max-height: 360px;
  overflow: auto;
  margin: 12px 0 0;
  padding: 14px;
  border: 1px solid var(--gray-150);
  border-radius: 8px;
  background: var(--gray-50);
  color: var(--gray-900);
  font-family: inherit;
  font-size: 13px;
  line-height: 1.65;
  white-space: pre-wrap;
  word-break: break-word;
}

.schema-description {
  margin-bottom: 8px;
  color: var(--gray-600);
  font-size: 13px;
  line-height: 1.5;
}

.form-grid.model-config-grid {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 180px;
  gap: 12px;

  @media (max-width: 640px) {
    grid-template-columns: 1fr;
  }
}

.form-item-hint {
  margin-top: 4px;
  font-size: 12px;
  line-height: 1.5;
  color: var(--gray-600, #6b7280);
}

.graph-config-footer {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.graph-config-footer-actions {
  display: flex;
  gap: 8px;
  margin-left: auto;
}

.settings-expand-enter-active,
.settings-expand-leave-active {
  transform-origin: top left;
  transition: transform 0.2s ease, opacity 0.2s ease;
}

.settings-expand-enter-from,
.settings-expand-leave-to {
  transform: translateY(-8px) scaleY(0.96);
  opacity: 0;
}

.slide-fade-enter-active {
  transition: all 0.25s ease-out;
}

.slide-fade-leave-active {
  transition: all 0.2s cubic-bezier(1, 0.5, 0.8, 1);
}

.slide-fade-enter-from,
.slide-fade-leave-to {
  transform: translateX(20px);
  opacity: 0;
}
</style>
