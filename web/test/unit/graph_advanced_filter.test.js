import assert from 'node:assert/strict'
import test from 'node:test'

import {
  availableAttributeNames,
  buildSubgraphPayload
} from '../../src/utils/graph_query_payload.js'

test('请求体丢弃空行与空值，且起点终点只保留 entity_id', () => {
  const payload = buildSubgraphPayload({
    kbId: 'kb_1',
    start: { entity_id: 'e_org', name: '待接入企业' },
    end: null,
    maxDepth: 3,
    maxNodes: 100,
    filters: {
      intermediateEntities: ['项目'],
      relationTypes: ['属于', '投资'],
      entityAttributes: [
        { name: '状态', value: '进行中' },
        { name: '', value: '' }
      ],
      relationAttributes: [{ name: 'text', value: '' }]
    }
  })

  assert.deepEqual(payload, {
    kb_id: 'kb_1',
    start: { entity_id: 'e_org' },
    end: null,
    max_depth: 3,
    max_nodes: 100,
    filters: {
      intermediate_entities: ['项目'],
      entity_attributes: [{ name: '状态', value: '进行中' }],
      relation_types: ['属于', '投资'],
      relation_attributes: []
    }
  })
})

test('只填终点时仍发出 null 起点，让后端给出明确错误', () => {
  const payload = buildSubgraphPayload({
    kbId: 'kb_1',
    start: null,
    end: { entity_id: 'e_person', name: '张三' },
    maxDepth: 2,
    maxNodes: 100,
    filters: {}
  })

  assert.equal(payload.start, null)
  assert.deepEqual(payload.end, { entity_id: 'e_person' })
})

test('已被占用的属性名不再出现在可选列表里', () => {
  const options = [{ name: '城市' }, { name: '状态' }]

  const remaining = availableAttributeNames(options, [{ name: '城市', value: '上海' }])

  assert.deepEqual(
    remaining.map((item) => item.name),
    ['状态']
  )
})

test('正在编辑的行保留它自己已选的属性名', () => {
  const options = [{ name: '城市' }, { name: '状态' }]
  const row = { name: '城市', value: '上海' }

  const remaining = availableAttributeNames(options, [row], row)

  assert.deepEqual(
    remaining.map((item) => item.name),
    ['城市', '状态']
  )
})
