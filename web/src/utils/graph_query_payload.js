/** 图谱查询请求体的纯函数装配，使判定逻辑可以脱离组件单独验证。 */

/** 把面板状态转成后端请求体；空行、空值与空字符串一律不发送。 */
export function buildSubgraphPayload({ kbId, start, end, maxDepth, maxNodes, filters = {} }) {
  return {
    kb_id: kbId,
    start: start ? { entity_id: start.entity_id } : null,
    end: end ? { entity_id: end.entity_id } : null,
    max_depth: maxDepth,
    max_nodes: maxNodes,
    filters: {
      intermediate_entities: [...(filters.intermediateEntities || [])],
      entity_attributes: toConditions(filters.entityAttributes),
      relation_types: [...(filters.relationTypes || [])],
      relation_attributes: toConditions(filters.relationAttributes)
    }
  }
}

/** 只保留同时填了属性名与属性值的行。 */
function toConditions(rows) {
  return (rows || [])
    .filter((row) => row && row.name && row.value)
    .map((row) => ({ name: row.name, value: row.value }))
}

/**
 * 返回去掉已被其它行占用的属性名之后的选项，避免同一个属性名加出恒空条件。
 * `editingRow` 是正在编辑的那一行：它自己已选的属性名必须保留，否则一选就消失。
 */
export function availableAttributeNames(optionItems, rows, editingRow = null) {
  const used = new Set(
    (rows || [])
      .filter((row) => row && row !== editingRow)
      .map((row) => row.name)
      .filter(Boolean)
  )
  return (optionItems || []).filter((item) => !used.has(item.name))
}
