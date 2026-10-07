import assert from 'node:assert/strict'
import test from 'node:test'

import {
  buildDatabaseRequest,
  createDerivedShareConfig,
  createEmptyDatabaseForm,
  selectDatabaseType,
  validateDatabaseConfig
} from '../../src/utils/databaseCreateForm.js'
import { getKbTypeLabel } from '../../src/utils/kb_utils.js'

const difyType = {
  create_params: {
    options: [
      { key: 'url', label: '地址', required: true },
      { key: 'token', label: 'Token', type: 'password', required: true }
    ]
  }
}

test('切换知识库类型保留通用字段并重置类型参数', () => {
  const form = { ...createEmptyDatabaseForm('embed/model'), name: '产品资料', description: '说明' }
  const selected = selectDatabaseType(form, 'dify', difyType)
  assert.equal(selected.name, '产品资料')
  assert.equal(selected.description, '说明')
  assert.deepEqual(selected.additional_params, { url: '', token: '' })
})

test('配置校验拒绝空名称和必填动态字段', () => {
  const empty = selectDatabaseType(createEmptyDatabaseForm(), 'dify', difyType)
  assert.equal(validateDatabaseConfig(empty, difyType), '请输入知识库名称')
  empty.name = '资料'
  assert.equal(validateDatabaseConfig(empty, difyType), '请填写地址')
})

test('只为需要嵌入模型的类型构建模型和分块参数', () => {
  const form = {
    ...createEmptyDatabaseForm('embed/model'),
    name: '资料',
    kb_type: 'milvus',
    chunk_preset_id: 'general'
  }
  const request = buildDatabaseRequest(
    form,
    { requires_embedding_model: true, create_params: { options: [] } },
    { version: 2 },
    'fallback/model'
  )
  assert.equal(request.embedding_model_spec, 'embed/model')
  assert.equal(request.additional_params.chunk_preset_id, 'general')

  const connectorRequest = buildDatabaseRequest(
    { ...form, kb_type: 'dify' },
    difyType,
    { version: 2 },
    'fallback/model'
  )
  assert.equal('embedding_model_spec' in connectorRequest, false)
  assert.equal('chunk_preset_id' in connectorRequest.additional_params, false)
})

test('请求体带上知识库范围，且普通用户的共享范围被收敛成指定用户', () => {
  const form = {
    ...createEmptyDatabaseForm('embed/model'),
    name: '我的资料',
    kb_type: 'milvus'
  }

  const personal = buildDatabaseRequest(
    form,
    { requires_embedding_model: true, create_params: { options: [] } },
    { version: 2 },
    'fallback/model',
    'personal'
  )
  assert.equal(personal.scope, 'personal')

  const shared = buildDatabaseRequest(
    form,
    { requires_embedding_model: true, create_params: { options: [] } },
    { version: 2 },
    'fallback/model',
    'shared'
  )
  assert.equal(shared.scope, 'shared')
})

test('未显式配置共享时不发送 share_config，显式配置时按用户选择发送', () => {
  const form = {
    ...createEmptyDatabaseForm('embed/model'),
    name: '共享库',
    kb_type: 'milvus'
  }
  const typeInfo = { requires_embedding_model: true, create_params: { options: [] } }

  // 未配置共享：不发送 share_config，交由后端按 scope 派生默认读取范围
  // （共享库→创建者所在部门；个人库→仅创建者），否则后端的部门默认永远不生效。
  const unconfigured = buildDatabaseRequest(form, typeInfo, null, 'fallback/model', 'shared')
  assert.equal('share_config' in unconfigured, false)

  // 用户显式配置：仍然按用户选择发送。
  const configured = buildDatabaseRequest(
    form,
    typeInfo,
    {
      version: 2,
      read_scope: { access_level: 'department', department_ids: [7], user_uids: [] },
      manage_scope: null
    },
    'fallback/model',
    'shared'
  )
  assert.equal(configured.share_config.read_scope.access_level, 'department')
  assert.deepEqual(configured.share_config.read_scope.department_ids, [7])
})

test('未交互时表单展示的默认共享范围与后端派生结果一致', () => {
  // 共享库 + 有部门 → 本部门；这是「共享库默认限本部门」在界面上的呈现，
  // 必须与 backend `_normalize_share_config` 的 None 分支一致。
  const shared = createDerivedShareConfig({ scope: 'shared', departmentId: 7, uid: 'u1' })
  assert.equal(shared.read_scope.access_level, 'department')
  assert.deepEqual(shared.read_scope.department_ids, [7])

  // 共享库 + 无部门 → 仅创建者，绝不 global。
  const sharedNoDepartment = createDerivedShareConfig({ scope: 'shared', departmentId: null, uid: 'u1' })
  assert.equal(sharedNoDepartment.read_scope.access_level, 'user')
  assert.deepEqual(sharedNoDepartment.read_scope.user_uids, ['u1'])

  // 个人库 → 仅创建者（不允许因为部门存在而放宽）。
  const personal = createDerivedShareConfig({ scope: 'personal', departmentId: 7, uid: 'u1' })
  assert.equal(personal.read_scope.access_level, 'user')
  assert.deepEqual(personal.read_scope.user_uids, ['u1'])

  for (const config of [shared, sharedNoDepartment, personal]) {
    assert.equal(config.version, 2)
    assert.equal(config.manage_scope, null)
    // read_scope 必须存在：否则 ShareConfigForm 会回落 global 并在挂载时 emit。
    assert.ok(config.read_scope)
    assert.notEqual(config.read_scope.access_level, 'global')
  }
})

test('知识库类型标签映射将 milvus 解析为研发猫', () => {
  assert.equal(getKbTypeLabel('milvus'), '研发猫')
  assert.equal(getKbTypeLabel('Milvus'), '研发猫')
  assert.equal(getKbTypeLabel('dify'), 'Dify')
  assert.equal(getKbTypeLabel('notion'), 'Notion')
  assert.equal(getKbTypeLabel('unknown'), 'unknown')
})
