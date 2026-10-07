// 后端 `_normalize_share_config` 在 `share_config` 为 None 时的派生结果（唯一事实来源）：
// 个人库 → 仅创建者；共享库有部门 → 本部门；共享库无部门 → 仅创建者；永不 global。
// 这里只用来驱动创建表单「权限」步的**默认显示**，让它与后端派生结果一致、绝不宽于后端；
// 真正发送与否由调用方决定（未交互时不发送 share_config，派生权威仍在后端）。
export const createDerivedShareConfig = ({ scope, departmentId, uid } = {}) => {
  const numericDepartmentId =
    departmentId === null || departmentId === undefined || departmentId === ''
      ? null
      : Number(departmentId)
  if (scope === 'shared' && Number.isFinite(numericDepartmentId)) {
    return {
      version: 2,
      read_scope: {
        access_level: 'department',
        department_ids: [numericDepartmentId],
        user_uids: []
      },
      manage_scope: null
    }
  }
  // 个人库、以及无部门的共享库都退到"仅创建者"。read_scope 必须非空，否则
  // ShareConfigForm 会回落 global 并在挂载时 emit，把派生权威搬到前端。
  return {
    version: 2,
    read_scope: {
      access_level: 'user',
      department_ids: [],
      user_uids: uid ? [String(uid)] : []
    },
    manage_scope: null
  }
}

export const createEmptyDatabaseForm = (embeddingModel = '') => ({
  name: '',
  description: '',
  embedding_model_spec: embeddingModel,
  kb_type: '',
  chunk_preset_id: DEFAULT_CHUNK_PRESET_ID,
  additional_params: {}
})

export const createParamValues = (fields = []) =>
  Object.fromEntries(
    fields.map((field) => [
      field.key,
      'default' in field ? field.default : field.type === 'boolean' ? false : ''
    ])
  )

export const selectDatabaseType = (form, type, typeInfo) => ({
  ...form,
  kb_type: type,
  additional_params: createParamValues(typeInfo?.create_params?.options)
})

export const validateDatabaseConfig = (form, typeInfo) => {
  if (!String(form?.name || '').trim()) return '请输入知识库名称'
  if (typeInfo?.requires_embedding_model && !form?.embedding_model_spec) {
    return '请选择嵌入模型'
  }

  for (const field of typeInfo?.create_params?.options || []) {
    const value = form?.additional_params?.[field.key]
    if (
      field.required &&
      (value === undefined || value === null || (typeof value === 'string' && !value.trim()))
    ) {
      return `请填写${field.label || field.key}`
    }
    if (field.type === 'number' && typeof value === 'number') {
      if (field.min !== undefined && value < field.min)
        return `${field.label || field.key}不能小于${field.min}`
      if (field.max !== undefined && value > field.max)
        return `${field.label || field.key}不能大于${field.max}`
    }
  }
  return ''
}

export const buildDatabaseRequest = (
  form,
  typeInfo,
  shareConfig,
  defaultEmbeddingModel,
  scope = 'shared'
) => {
  const additionalParams = {}
  for (const field of typeInfo?.create_params?.options || []) {
    const value = form.additional_params[field.key]
    additionalParams[field.key] = typeof value === 'string' ? value.trim() : value
  }

  const request = {
    database_name: form.name.trim(),
    description: form.description?.trim() || '',
    kb_type: form.kb_type,
    additional_params: additionalParams,
    scope
  }
  // shareConfig 为 null/undefined 表示用户没有显式配置共享：不发送该字段，
  // 让后端 `_normalize_share_config` 按 scope 派生默认读取范围
  // （个人库→仅创建者；共享库→创建者所在部门；无部门→仅创建者，绝不 global）。
  if (shareConfig) {
    request.share_config = shareConfig
  }
  if (typeInfo?.requires_embedding_model) {
    request.embedding_model_spec = form.embedding_model_spec || defaultEmbeddingModel
    request.additional_params.chunk_preset_id = form.chunk_preset_id || DEFAULT_CHUNK_PRESET_ID
  }
  return request
}
import { DEFAULT_CHUNK_PRESET_ID } from './chunkUtils.js'
