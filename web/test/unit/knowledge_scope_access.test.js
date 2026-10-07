import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

const source = readFileSync(new URL('../../src/apis/knowledge_api.js', import.meta.url), 'utf8')

/** 取出某个方法定义到下一个 `: async` 之间的源码片段。 */
function methodSource(name, from = 0) {
  const start = source.indexOf(`${name}: async`, from)
  assert.ok(start >= 0, `未找到方法 ${name}`)
  const rest = source.slice(start)
  const next = rest.indexOf('async', rest.indexOf('=>'))
  const end = next >= 0 ? rest.indexOf('async (', next) : rest.length
  return end > 0 ? rest.slice(0, end) : rest
}

/** 取出某个 API 分组（`export const xxx = {` 到该分组闭合的 `\n}`）的源码片段。 */
function blockSource(name) {
  const start = source.indexOf(`export const ${name} = {`)
  assert.ok(start >= 0, `未找到分组 ${name}`)
  const end = source.indexOf('\n}', start)
  return end > 0 ? source.slice(start, end) : source.slice(start)
}

/** KB 级方法：端点是某个具体知识库的能力。授权在后端按库判定，前端不得预判。 */
const KB_LEVEL_METHODS = [
  'getDatabaseInfo',
  'repairDatabaseStats',
  'detectVirtualFolders',
  'startVirtualFolderMigration',
  'streamVirtualFolderMigration',
  'updateDatabase',
  'deleteDatabase',
  'generateDescription',
  'listDocuments',
  'searchDocuments',
  'documentExists',
  'createFolder',
  'renameFolder',
  'moveDocument',
  'addDocuments',
  'addUploadedDocuments',
  'getDocumentInfo',
  'getDocumentBasicInfo',
  'getDocumentContent',
  'deleteDocument',
  'downloadDocument',
  'parseDocuments',
  'parsePendingDocuments',
  'indexDocuments',
  'indexPendingDocuments',
  'getStatus',
  'getFailedChunks',
  'configure',
  'startIndex',
  'reset',
  'reconcile',
  'backfillAttributes',
  'getDatabaseFiles',
  'generateMindmap',
  'getByDatabase',
  'getDiff',
  'queryKnowledgeBase',
  'queryTest',
  'getKnowledgeBaseQueryParams',
  'updateKnowledgeBaseQueryParams',
  'generateSampleQuestions',
  'getSampleQuestions',
  'fetchUrl',
  'importWorkspaceFiles',
  'uploadFile',
  'getSupportedFileTypes',
  'processFolder',
  'getChunkPresets'
]

test('建库接口不再走管理员通道', () => {
  assert.ok(!methodSource('createDatabase').includes('apiAdminPost'))
})

test('知识库类型接口不再走管理员通道', () => {
  assert.ok(!methodSource('getKnowledgeBaseTypes').includes('apiAdminGet'))
})

test('全部 KB 级调用改走普通传输层，不再客户端预判管理员权限', () => {
  for (const name of KB_LEVEL_METHODS) {
    const method = methodSource(name)
    assert.ok(!method.includes('apiAdmin'), `${name} 仍在客户端预判管理员权限`)
    assert.match(method, /api(Get|Post|Put|Delete)\(/, `${name} 未走普通传输层`)
  }
})

test('带响应类型的调用把 responseType 传在第 4 位', () => {
  // apiGet 的签名是 (url, options, requiresAuth, responseType)，而 apiAdminGet 是
  // (url, options, responseType)：换底座时若照抄 `{}, 'blob'` 会把响应类型传进认证位，
  // 请求仍会发出、响应却按 JSON 解析，是运行期才暴露的静默错误。
  assert.match(methodSource('downloadDocument'), /apiGet\([^)]*,\s*true,\s*'blob'\s*\)/)
  assert.match(methodSource('streamVirtualFolderMigration'), /apiGet\([^)]*,\s*true,\s*'response'\s*\)/)
})

test('管理级调用仍走管理员通道', () => {
  // 跨知识库的全量/聚合视图：普通用户走各自的可访问列表，不得放开。
  assert.ok(methodSource('getDatabases').includes('apiAdminGet'))
  assert.ok(methodSource('getStatistics').includes('apiAdminGet'))
  assert.ok(
    methodSource('getDatabases', source.indexOf('export const mindmapApi = {')).includes('apiAdminGet')
  )

  // 评估接口全部保持管理员通道（评估数据集走各自的适配器）。
  const evaluation = blockSource('evaluationApi')
  assert.equal((evaluation.match(/apiAdmin/g) || []).length, 11)
  assert.ok(!/api(Get|Post|Put|Delete)\(/.test(evaluation), '评估接口不应走普通传输层')
})
