import assert from 'node:assert/strict'
import test from 'node:test'

import { isOwnPersonalDatabase } from '../../src/utils/kb_utils.js'

// 知识库列表按「是否我自己的个人库」分段，判据是归属：scope 为 personal 且
// created_by 是我。can_manage 不能当归属依据——管理员对所有库都是 true。
const ME = 'u-me'
const OTHER = 'u-other'

test('自己创建的个人库算作我的个人知识库', () => {
  assert.equal(isOwnPersonalDatabase({ scope: 'personal', created_by: ME }, ME), true)
})

test('别人创建的个人库不算我的，即使我对它有管理权限', () => {
  assert.equal(
    isOwnPersonalDatabase({ scope: 'personal', created_by: OTHER, can_manage: true }, ME),
    false
  )
})

test('共享库不算我的个人知识库', () => {
  assert.equal(isOwnPersonalDatabase({ scope: 'shared', created_by: ME }, ME), false)
})

test('没有归属的库不算我的个人知识库', () => {
  assert.equal(isOwnPersonalDatabase({ scope: 'personal', created_by: null }, ME), false)
  assert.equal(isOwnPersonalDatabase({ scope: 'personal', created_by: undefined }, ME), false)
})

test('判据只看归属，不看管理权限', () => {
  assert.equal(
    isOwnPersonalDatabase({ scope: 'personal', created_by: ME, can_manage: false }, ME),
    true
  )
})

test('缺字段的列表项不会误判为我的个人知识库', () => {
  assert.equal(isOwnPersonalDatabase(undefined, ME), false)
  assert.equal(isOwnPersonalDatabase({}, ME), false)
})
