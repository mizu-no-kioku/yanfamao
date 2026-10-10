import assert from 'node:assert/strict'
import test from 'node:test'

import { cloneScope, getShareConfigLabel, isGlobalScope } from '../../src/utils/shareConfig.js'
import { createDerivedShareConfig } from '../../src/utils/databaseCreateForm.js'

const readScope = (levels, departments = [], users = []) => ({
  access_levels: levels,
  department_ids: departments,
  user_uids: users
})

test('并集范围同时显示两类对象', () => {
  assert.equal(
    getShareConfigLabel({
      version: 2,
      read_scope: readScope(['department', 'user'], [2], ['u-7']),
      manage_scope: null
    }),
    '只读部门(1) · 用户(1)'
  )
})

test('单档与全局保持既有文案', () => {
  assert.equal(
    getShareConfigLabel({ version: 2, read_scope: readScope(['global']), manage_scope: null }),
    '只读全局'
  )
  assert.equal(
    getShareConfigLabel({
      version: 2,
      read_scope: readScope(['department'], [2, 3]),
      manage_scope: null
    }),
    '只读部门(2)'
  )
})

test('旧形状（单个 access_level）仍能显示', () => {
  assert.equal(
    getShareConfigLabel({
      version: 2,
      read_scope: { access_level: 'global', department_ids: [], user_uids: [] }
    }),
    '只读全局'
  )
})

test('建库派生默认值产出新形状', () => {
  assert.deepEqual(
    createDerivedShareConfig({ scope: 'shared', departmentId: 2, uid: 'u-1' }).read_scope,
    {
      access_levels: ['department'],
      department_ids: [2],
      user_uids: []
    }
  )
  assert.deepEqual(createDerivedShareConfig({ scope: 'personal', uid: 'u-1' }).read_scope, {
    access_levels: ['user'],
    department_ids: [],
    user_uids: ['u-1']
  })
})

test('「是不是全局共享」只由档位数组判定', () => {
  // 「全局共享」计数与标记都用这个判据；并集（含 department/user）不是全局，真全局不能漏算。
  assert.equal(isGlobalScope(readScope(['global'])), true)
  assert.equal(isGlobalScope({}), false)
  assert.equal(isGlobalScope(readScope(['department'], [2])), false)
  assert.equal(isGlobalScope(readScope(['department', 'user'], [2], ['u-7'])), false)
  assert.equal(isGlobalScope({ access_level: 'global', department_ids: [], user_uids: [] }), true)
  assert.equal(isGlobalScope(null), false)
})

test('cloneScope 把形状收成合法表达', () => {
  // 含 global 时必须是唯一档且两份名单为空——不得留下其它档位的名单。
  assert.deepEqual(cloneScope(readScope(['global'], [3], ['u-1'])), {
    access_levels: ['global'],
    department_ids: [],
    user_uids: []
  })
  // 读不到档位：用兜底档位，并按兜底档位清掉不属于它的名单。
  assert.deepEqual(cloneScope({ department_ids: [3], user_uids: ['u-1'] }, ['user']), {
    access_levels: ['user'],
    department_ids: [],
    user_uids: ['u-1']
  })
  // 并集保留两份名单；旧形状也认。
  assert.deepEqual(cloneScope(readScope(['department', 'user'], [2], ['u-7'])), {
    access_levels: ['department', 'user'],
    department_ids: [2],
    user_uids: ['u-7']
  })
  assert.deepEqual(cloneScope({ access_level: 'department', department_ids: [2], user_uids: [] }), {
    access_levels: ['department'],
    department_ids: [2],
    user_uids: []
  })
  assert.equal(cloneScope(null), null)
})
