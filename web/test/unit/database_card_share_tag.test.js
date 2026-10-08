import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

import { getShareTagLabel } from '../../src/utils/shareConfig.js'

// 知识库卡片的共享范围标签。后端在缺 share_config 时不下发该字段，前端不能拿空对象去拼
// 标签——那会渲染出「只读用户(0)」这种假信息（上报的缺陷现场）。
// 共享范围本身不按权限裁剪：只读用户拿到的是真实的库范围。

const SHARED_TO_DEPARTMENT = {
  version: 2,
  read_scope: { access_level: 'department', department_ids: [7], user_uids: [] },
  manage_scope: null
}

test('有共享范围时按范围给出真实标签', () => {
  assert.equal(getShareTagLabel({ share_config: SHARED_TO_DEPARTMENT }), '只读部门(1)')
})

test('缺共享范围时不产生标签，空对象也算缺', () => {
  assert.equal(getShareTagLabel({ kb_id: 'kb-1' }), null)
  assert.equal(getShareTagLabel({ share_config: null }), null)
  assert.equal(getShareTagLabel({ share_config: {} }), null)
  assert.equal(getShareTagLabel({}), null)
  assert.equal(getShareTagLabel(undefined), null)
})

test('知识库列表卡片的共享标签只由这个判据决定', () => {
  const source = readFileSync(new URL('../../src/views/DataBaseView.vue', import.meta.url), 'utf8')

  assert.match(source, /getShareTagLabel\(database\)/)
  assert.doesNotMatch(source, /name: getShareConfigLabel\(database\.share_config\)/)
})
