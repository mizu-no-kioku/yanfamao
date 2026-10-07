import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

const source = readFileSync(new URL('../../src/apis/knowledge_api.js', import.meta.url), 'utf8')

/** 取出某个方法定义到下一个 `: async` 之间的源码片段。 */
function methodSource(name) {
  const start = source.indexOf(`${name}: async`)
  assert.ok(start >= 0, `未找到方法 ${name}`)
  const rest = source.slice(start)
  const next = rest.indexOf('async', rest.indexOf('=>'))
  const end = next >= 0 ? rest.indexOf('async (', next) : rest.length
  return end > 0 ? rest.slice(0, end) : rest
}

test('建库接口不再走管理员通道', () => {
  assert.ok(!methodSource('createDatabase').includes('apiAdminPost'))
})

test('知识库类型接口不再走管理员通道', () => {
  assert.ok(!methodSource('getKnowledgeBaseTypes').includes('apiAdminGet'))
})
