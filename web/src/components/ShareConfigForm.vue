<template>
  <div class="share-config-form" :class="{ disabled }">
    <a-alert
      v-if="hasManageScopeViolation"
      type="warning"
      show-icon
      message="当前管理权限范围大于读取权限范围，请调整后再保存。"
    />

    <section v-for="scope in scopeOptions" :key="scope.key" class="permission-scope-section">
      <div class="permission-scope-header" :class="{ 'has-content': Boolean(scopes[scope.key]) }">
        <h4>{{ scope.title }}</h4>
        <a-switch
          size="small"
          v-if="scope.key !== 'read_scope' || !requireReadScope"
          :checked="Boolean(scopes[scope.key])"
          :aria-label="`${scope.title}${scopes[scope.key] ? '已开启' : '已关闭'}`"
          :disabled="disabled || (scope.key === 'read_scope' && requireReadScope)"
          @change="(enabled) => toggleScope(scope.key, enabled)"
        />
      </div>
      <template v-if="scopes[scope.key]">
        <div
          class="share-mode-cards"
          role="radiogroup"
          :aria-label="scope.title"
        >
          <div
            v-for="option in shareModeOptions"
            :key="option.value"
            role="radio"
            class="share-mode-card"
            :class="{ active: scopes[scope.key].access_levels.includes(option.value) }"
            :aria-checked="scopes[scope.key].access_levels.includes(option.value)"
            :tabindex="!disabled && scopes[scope.key].access_levels.includes(option.value) ? 0 : -1"
            @click="toggleAccessLevel(scope.key, option.value)"
            @keydown.enter.prevent="toggleAccessLevel(scope.key, option.value)"
            @keydown.space.prevent="toggleAccessLevel(scope.key, option.value)"
          >
            <div class="card-main">
              <div class="card-header">
                <div class="card-icon-wrapper" aria-hidden="true">
                  <component :is="option.icon" class="card-icon" :size="20" />
                </div>
                <div class="card-title">{{ option.title }}</div>
                <div
                  v-if="
                    scopes[scope.key].access_levels.includes(option.value) && option.value !== 'global'
                  "
                  class="card-action"
                  @click.stop
                >
                  <a-dropdown
                    :trigger="['click']"
                    placement="bottomRight"
                    overlay-class-name="share-selection-popover"
                  >
                    <a-button
                      size="small"
                      class="select-action lucide-icon-btn"
                      :aria-label="option.value === 'department' ? '选择部门' : '选择用户'"
                      :disabled="disabled"
                    >
                      <UserPlus class="select-action-icon" :size="14" />
                      <span class="access-count">{{
                        getAccessCount(scope.key, option.value)
                      }}</span>
                    </a-button>
                    <template #overlay>
                      <div class="selection-dropdown" @mousedown.stop @click.stop>
                        <div class="selection-dropdown-header">
                          <div class="selection-dropdown-title">
                            {{ scope.key === 'manage_scope' ? '可管理' : '可读取'
                            }}{{ option.value === 'department' ? '部门' : '用户' }}
                          </div>
                          <div class="selection-dropdown-subtitle">
                            {{ getAccessSummary(scope.key, option.value) }}
                          </div>
                        </div>
                        <a-input
                          v-model:value="selectionSearch[scope.key][option.value]"
                          size="small"
                          allow-clear
                          class="selection-search"
                          :placeholder="option.value === 'department' ? '搜索部门' : '搜索用户'"
                          @mousedown.stop
                          @click.stop
                        />
                        <div
                          v-if="getSelectionOptions(scope.key, option.value).length"
                          class="selection-list"
                        >
                          <div
                            v-for="item in getSelectionOptions(scope.key, option.value)"
                            :key="item.value"
                            role="checkbox"
                            :aria-checked="isSelected(scope.key, option.value, item.value)"
                            tabindex="0"
                            class="selection-item"
                            :class="{ selected: isSelected(scope.key, option.value, item.value) }"
                            @mousedown.stop
                            @click.stop="
                              toggleSelection(
                                scope.key,
                                option.value,
                                item.value,
                                !isSelected(scope.key, option.value, item.value)
                              )
                            "
                            @keydown.enter.prevent="
                              toggleSelection(
                                scope.key,
                                option.value,
                                item.value,
                                !isSelected(scope.key, option.value, item.value)
                              )
                            "
                            @keydown.space.prevent="
                              toggleSelection(
                                scope.key,
                                option.value,
                                item.value,
                                !isSelected(scope.key, option.value, item.value)
                              )
                            "
                          >
                            <span class="selection-item-content">
                              <a-checkbox
                                :checked="isSelected(scope.key, option.value, item.value)"
                                @click.stop
                                @change="
                                  toggleSelection(
                                    scope.key,
                                    option.value,
                                    item.value,
                                    $event.target.checked
                                  )
                                "
                              />
                              <span class="selection-label">{{ item.label }}</span>
                            </span>
                          </div>
                        </div>
                        <div v-else class="selection-empty">暂无可选项</div>
                      </div>
                    </template>
                  </a-dropdown>
                </div>
              </div>
              <div class="card-description">{{ option.description }}</div>
            </div>
          </div>
        </div>
      </template>
    </section>

    <a-alert
      v-if="disabled && disabledReason"
      type="info"
      show-icon
      class="share-disabled-alert"
      :message="disabledReason"
    />
  </div>
</template>

<script setup>
import { computed, nextTick, onMounted, reactive, ref, watch } from 'vue'
import { Building2, Globe, Users, UserPlus } from '@lucide/vue'
import { useUserStore } from '@/stores/user'
import { authApi } from '@/apis/auth_api'
import { departmentApi } from '@/apis/department_api'
import { toAccessLevels } from '@/utils/shareConfig'

const userStore = useUserStore()
const departments = ref([])
const users = ref([])
const syncingFromProps = ref(false)

const props = defineProps({
  modelValue: {
    type: Object,
    required: true,
    default: () => ({
      version: 2,
      read_scope: { access_levels: ['global'], department_ids: [], user_uids: [] },
      manage_scope: null
    })
  },
  autoSelectUserDept: { type: Boolean, default: false },
  disabled: { type: Boolean, default: false },
  disabledReason: { type: String, default: '' },
  requireReadScope: { type: Boolean, default: false },
  allowedAccessLevels: {
    type: Array,
    default: () => ['global', 'department', 'user']
  }
})

const emit = defineEmits(['update:modelValue'])

const scopeOptions = [
  { key: 'read_scope', title: '读取权限' },
  { key: 'manage_scope', title: '共享管理权限（包含读取权限）' }
]

const baseShareModeOptions = [
  { value: 'global', title: '全局共享', description: '所有用户都可以访问', icon: Globe },
  {
    value: 'department',
    title: '部门共享',
    description: '选中的部门成员可以访问',
    icon: Building2
  },
  { value: 'user', title: '指定人', description: '选中的用户可以访问', icon: Users }
]

const scopes = reactive({ read_scope: null, manage_scope: null })
const selectionSearch = reactive({
  read_scope: { department: '', user: '' },
  manage_scope: { department: '', user: '' }
})

const currentDepartmentId = computed(() =>
  userStore.departmentId ? Number(userStore.departmentId) : null
)
const currentUserUid = computed(() => userStore.uid || '')
const normalizedAllowedAccessLevels = computed(() => {
  const allowed = props.allowedAccessLevels.filter((level) =>
    ['global', 'department', 'user'].includes(level)
  )
  return allowed.length ? allowed : ['global']
})
const shareModeOptions = computed(() =>
  baseShareModeOptions.filter((option) =>
    normalizedAllowedAccessLevels.value.includes(option.value)
  )
)

const createScope = (scope) => ({
  access_levels: toAccessLevels(scope),
  department_ids: Array.from(
    new Set((scope?.department_ids || []).map(Number).filter(Number.isFinite))
  ),
  user_uids: Array.from(
    new Set((scope?.user_uids || []).map((uid) => String(uid).trim()).filter(Boolean))
  )
})

const normalizeScope = (scope, { includeCurrent = false } = {}) => {
  if (!scope) return null
  const normalized = createScope(scope)
  const levels = normalized.access_levels.filter((level) =>
    normalizedAllowedAccessLevels.value.includes(level)
  )
  if (!levels.length) return null
  if (levels.includes('global')) {
    return { access_levels: ['global'], department_ids: [], user_uids: [] }
  }
  // 只在该档名单为空时补当前主体：它的作用是让范围合法，不覆盖已显式选好的名单。
  if (
    includeCurrent &&
    levels.includes('department') &&
    currentDepartmentId.value &&
    !normalized.department_ids.length
  ) {
    normalized.department_ids = [currentDepartmentId.value]
  }
  if (
    includeCurrent &&
    levels.includes('user') &&
    currentUserUid.value &&
    !normalized.user_uids.length
  ) {
    normalized.user_uids = [currentUserUid.value]
  }
  normalized.access_levels = ['department', 'user'].filter((level) => levels.includes(level))
  if (levels.includes('department') && !normalized.department_ids.length) return null
  if (levels.includes('user') && !normalized.user_uids.length) return null
  return normalized
}

const isManageScopeWithinRead = (manageScope, readScope = scopes.read_scope) => {
  if (!manageScope || !readScope) return true
  const readLevels = toAccessLevels(readScope)
  if (readLevels.includes('global')) return true
  const manageLevels = toAccessLevels(manageScope)
  if (!manageLevels.length || !manageLevels.every((level) => readLevels.includes(level))) {
    return false
  }
  if (
    manageLevels.includes('user') &&
    !manageScope.user_uids.every((uid) => readScope.user_uids.includes(uid))
  ) {
    return false
  }
  if (
    manageLevels.includes('department') &&
    !manageScope.department_ids.every((id) => readScope.department_ids.includes(id))
  ) {
    return false
  }
  return true
}

const initConfig = () => {
  syncingFromProps.value = true
  const source = props.modelValue || {}
  const isV2 = source.version === 2
  const readScope = isV2 ? source.read_scope : source
  scopes.read_scope = normalizeScope(readScope, { includeCurrent: props.autoSelectUserDept })
  scopes.manage_scope = normalizeScope(isV2 ? source.manage_scope : null)
  // 响应里没有可用的 read_scope 时保持为 null，不凭空发明全局范围——那会把一个没有
  // 读取范围的知识库改写成全局可见。由 validate() 挡住保存。
  nextTick(() => {
    syncingFromProps.value = false
  })
}

// 输出前清掉「已取消档位」的残留名单：合法表达要求「不含 department ⇒ department_ids 为空」，
// 残留会被后端严格归一拒绝。表单内部仍留着名单，用户把该档选回来时原选择还在。
const toPayloadScope = (scope) => {
  if (!scope) return null
  const levels = toAccessLevels(scope)
  return {
    access_levels: levels,
    department_ids: levels.includes('department') ? scope.department_ids : [],
    user_uids: levels.includes('user') ? scope.user_uids : []
  }
}

const emitConfig = () => {
  const readScope = normalizeScope(scopes.read_scope)
  const manageScope = normalizeScope(scopes.manage_scope)
  // 归一不出合法范围时不 emit：保留表单里的非法状态与上一次已提交的值，由 validate() 说明原因。
  if (scopes.read_scope && !readScope) return
  if (scopes.manage_scope && !manageScope) return
  emit('update:modelValue', {
    version: 2,
    read_scope: toPayloadScope(readScope),
    manage_scope: toPayloadScope(manageScope)
  })
}

const toggleScope = (scopeKey, enabled) => {
  if (props.disabled) return
  if (!enabled) {
    scopes[scopeKey] = null
    return
  }
  const allowed = normalizedAllowedAccessLevels.value
  const readLevels = toAccessLevels(scopes.read_scope).filter((level) => allowed.includes(level))
  const seedLevels =
    scopeKey === 'manage_scope' && readLevels.length
      ? readLevels
      : [allowed.includes('global') ? 'global' : allowed[0]]
  // 开启时给一个合法默认：管理范围沿用读取范围的档位，读取范围取首个可用档；两种都补上
  // 当前部门/本人——新形状要求「含某档 ⇒ 该档名单非空」，空名单的默认范围没有合法表达，
  // 否则开关一开就被归一回 null，变成死开关。
  scopes[scopeKey] = normalizeScope({ access_levels: seedLevels }, { includeCurrent: true })
}

const toggleAccessLevel = (scopeKey, accessLevel) => {
  if (
    props.disabled ||
    !scopes[scopeKey] ||
    !normalizedAllowedAccessLevels.value.includes(accessLevel)
  )
    return
  // 切到「部门共享」时补一次：加载失败后，这是同一次弹窗会话里唯一可达的重试时机
  //（详情页传的是实例内稳定的数组，重渲染不会再触发 allowedAccessLevels 的 watcher）。
  if (accessLevel === 'department') ensureDepartmentsLoaded()
  const current = createScope(scopes[scopeKey])
  if (accessLevel === 'global') {
    scopes[scopeKey] = { access_levels: ['global'], department_ids: [], user_uids: [] }
    return
  }
  const has = current.access_levels.includes(accessLevel)
  if (has && current.access_levels.length === 1) return // 最后一档不取消
  current.access_levels = has
    ? current.access_levels.filter((level) => level !== accessLevel)
    : [...current.access_levels, accessLevel]
  scopes[scopeKey] = normalizeScope(current, {
    includeCurrent: scopeKey === 'read_scope' && props.autoSelectUserDept
  })
}

const departmentOptions = computed(() =>
  departments.value.map((dept) => ({
    label: dept.name,
    value: Number(dept.id)
  }))
)
const userOptions = computed(() =>
  users.value.map((user) => ({
    label: user.department_name ? `${user.username}（${user.department_name}）` : user.username,
    value: user.uid,
    department_id: user.department_id
  }))
)

const getAccessCount = (scopeKey, accessLevel) => {
  const scope = scopes[scopeKey]
  if (accessLevel === 'department') return scope?.department_ids.length || 0
  if (accessLevel === 'user') return scope?.user_uids.length || 0
  return ''
}
const getAccessSummary = (scopeKey, accessLevel) => {
  const scope = scopes[scopeKey]
  if (accessLevel === 'global') return '所有用户可访问'
  if (accessLevel === 'department') return `${scope?.department_ids.length || 0} 个部门可访问`
  return `${scope?.user_uids.length || 0} 个用户可访问`
}
const getSelectionOptions = (scopeKey, accessLevel) => {
  let options = accessLevel === 'department' ? departmentOptions.value : userOptions.value

  const query = selectionSearch[scopeKey][accessLevel].trim().toLowerCase()
  return query ? options.filter((item) => item.label.toLowerCase().includes(query)) : options
}
const isSelected = (scopeKey, accessLevel, value) => {
  const scope = scopes[scopeKey]
  if (!scope) return false
  return accessLevel === 'department'
    ? scope.department_ids.includes(Number(value))
    : scope.user_uids.includes(String(value))
}
const toggleSelection = (scopeKey, accessLevel, value, checked) => {
  if (props.disabled || !scopes[scopeKey]) return
  const scope = scopes[scopeKey]
  if (accessLevel === 'department') {
    scope.department_ids = Array.from(
      new Set(
        checked
          ? [...scope.department_ids, Number(value)]
          : scope.department_ids.filter((id) => id !== Number(value))
      )
    )
  } else {
    scope.user_uids = Array.from(
      new Set(
        checked
          ? [...scope.user_uids, String(value)]
          : scope.user_uids.filter((uid) => uid !== String(value))
      )
    )
  }
}

const loadDepartments = async () => {
  try {
    const result = await departmentApi.getDepartments()
    departments.value = result.departments || result || []
    return true
  } catch (error) {
    console.error('加载部门列表失败:', error)
    return false
  }
}
const loadUsers = async () => {
  try {
    users.value = await authApi.getUserAccessOptions()
  } catch (error) {
    console.error('加载用户列表失败:', error)
  }
}

const departmentsRequested = ref(false)
// 只在「能提供部门级」且尚未请求过时加载。departmentApi.getDepartments 走 apiAdmin 传输层，
// 普通用户在发请求之前就会被 checkAdminPermission() 抛错，所以不能无条件调用；同时不能只在
// 挂载时判断一次——个人库的实例可能在挂载后才放宽为共享库，那时部门选择器需要数据。
const ensureDepartmentsLoaded = async () => {
  if (departmentsRequested.value) return
  if (!normalizedAllowedAccessLevels.value.includes('department')) return
  departmentsRequested.value = true
  // 请求失败时复位，否则选择器会永久停在「暂无可选项」、validate() 挡住保存。
  if (!(await loadDepartments())) departmentsRequested.value = false
}

watch(() => props.modelValue, initConfig, { deep: true })
watch(normalizedAllowedAccessLevels, () => {
  initConfig()
  ensureDepartmentsLoaded()
})
watch(
  scopes,
  () => {
    if (!syncingFromProps.value) emitConfig()
  },
  { deep: true }
)

const validateScope = (scope, title) => {
  if (!scope) return { valid: true, message: '' }
  const levels = toAccessLevels(scope)
  if (!levels.length) return { valid: false, message: `${title}至少选择一种共享方式` }
  if (levels.includes('global')) return { valid: true, message: '' }
  if (levels.includes('department') && !scope.department_ids.length) {
    return { valid: false, message: `${title}至少需要选择一个部门` }
  }
  if (levels.includes('user') && !scope.user_uids.length) {
    return { valid: false, message: `${title}至少需要选择一个用户` }
  }
  return { valid: true, message: '' }
}

const validate = () => {
  if (props.requireReadScope && !scopes.read_scope) {
    return { valid: false, message: '读取权限至少选择一种共享方式' }
  }
  const readResult = validateScope(scopes.read_scope, '读取权限')
  if (!readResult.valid) return readResult
  const manageResult = validateScope(scopes.manage_scope, '管理权限')
  if (!manageResult.valid) return manageResult
  if (!isManageScopeWithinRead(scopes.manage_scope)) {
    return { valid: false, message: '管理权限必须包含在读取权限范围内' }
  }
  return manageResult
}

const hasManageScopeViolation = computed(() =>
  Boolean(scopes.manage_scope && !isManageScopeWithinRead(scopes.manage_scope))
)

onMounted(() => {
  initConfig()
  ensureDepartmentsLoaded()
  loadUsers()
})

defineExpose({ scopes, validate })
</script>

<style lang="less" scoped>
.share-config-form {
  display: flex;
  flex-direction: column;
  border-top: 1px solid var(--gray-150);
}

.permission-scope-section {
  padding: 14px 0;
  border: 0;
  border-radius: 0;
  background: transparent;
}

.permission-scope-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;

  &.has-content {
    margin-bottom: 12px;
  }
}

.permission-scope-header h4 {
  margin: 0;
  color: var(--gray-800);
  font-size: 14px;
}

.share-mode-cards {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 10px;
}

.share-mode-card {
  min-width: 0;
  padding: 12px;
  border: 1px solid var(--gray-200);
  border-radius: 10px;
  background: var(--gray-0);
  cursor: pointer;
}

.share-mode-card:hover,
.share-mode-card.active {
  border-color: var(--main-color);
  background: var(--main-10);
}

.card-header,
.card-main {
  display: flex;
  align-items: center;
  gap: 8px;
}

.card-main {
  align-items: stretch;
  flex-direction: column;
}

.card-title {
  flex: 1;
  color: var(--gray-800);
  font-size: 13px;
  font-weight: 600;
  line-height: 20px;
  height: 20px;
  display: flex;
  align-items: center;
}

.card-description {
  color: var(--gray-500);
  font-size: 12px;
  line-height: 1.45;
}

.card-icon-wrapper {
  display: inline-flex;
  align-items: center;
  color: var(--main-color);
  height: 20px;
}

.card-action {
  margin-left: auto;
  display: flex;
  align-items: center;
  height: 20px;

  :deep(.ant-btn) {
    height: 20px;
    padding: 0 8px;
    font-size: 12px;
    line-height: 1;
  }
}

.access-count {
  margin-left: 3px;
}

.share-disabled-alert {
  margin-top: 2px;
}

.selection-dropdown {
  width: 280px;
  padding: 10px;
  border: 1px solid var(--gray-200);
  border-radius: 8px;
  background: var(--gray-0);
  box-shadow: 0 8px 24px rgb(0 0 0 / 12%);
}

.selection-dropdown-header {
  margin-bottom: 8px;
}

.selection-dropdown-title {
  color: var(--gray-800);
  font-size: 13px;
  font-weight: 600;
}

.selection-dropdown-subtitle,
.selection-empty {
  color: var(--gray-500);
  font-size: 12px;
}

.selection-search {
  margin-bottom: 8px;
}

.selection-list {
  max-height: 240px;
  overflow-y: auto;
}

.selection-item {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  padding: 7px 4px;
  border-radius: 6px;
  cursor: pointer;
}

.selection-item:hover,
.selection-item.selected {
  background: var(--main-10);
}

.selection-item-content {
  display: flex;
  min-width: 0;
  align-items: center;
  gap: 6px;
}

.selection-label {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

@media (max-width: 768px) {
  .share-mode-cards {
    grid-template-columns: 1fr;
  }
}
</style>
