export function getShareConfigLabel(shareConfig) {
  const config = shareConfig || {}
  const readScope = config.version === 2 ? config.read_scope : config
  const manageScope = config.manage_scope
  if (config.version === 2 && !config.read_scope && !manageScope) return '仅所有者'
  const scopeLabel = (scope) => {
    const levels = toAccessLevels(scope)
    if (!levels.length) return '无'
    if (levels.includes('global')) return '全局'
    const parts = []
    if (levels.includes('department')) parts.push(`部门(${scope.department_ids?.length || 0})`)
    if (levels.includes('user')) parts.push(`用户(${scope.user_uids?.length || 0})`)
    return parts.join(' · ')
  }
  return manageScope
    ? `读${scopeLabel(readScope)} · 管${scopeLabel(manageScope)}`
    : `只读${scopeLabel(readScope)}`
}

/**
 * 知识库卡片上的共享范围标签；没有可用的共享范围时返回 null，由调用方跳过这个标签。
 *
 * 判据是「有没有一份非空的 share_config」而不是库的权限：拿空对象去拼只会得到
 * 「只读用户(0)」这种假信息，那正是缺字段时的旧兜底行为。
 */
export function getShareTagLabel(database) {
  const config = database?.share_config
  if (!config || !Object.keys(config).length) return null
  return getShareConfigLabel(config)
}

/** 读取范围档位；旧形状（单个 access_level）也接受——缓存里的响应可能是旧的。 */
export const toAccessLevels = (scope) => {
  if (!scope) return []
  const raw = scope.access_levels ?? (scope.access_level ? [scope.access_level] : [])
  return [
    ...new Set((Array.isArray(raw) ? raw : []).filter((l) => ['global', 'department', 'user'].includes(l)))
  ]
}

/** 是否全局共享；只按档位数组判定，并集不是全局。 */
export const isGlobalScope = (scope) => toAccessLevels(scope).includes('global')

/**
 * 克隆一份共享范围，并把形状收成合法表达：读不到档位时用 `fallbackLevels` 兜底，且
 * 清掉不属于所保留档位的名单——「含 global 必须唯一」「不含 department ⇒ department_ids 为空」
 * 是后端认的合法表达，前端不得产出反例。
 */
export const cloneScope = (scope, fallbackLevels = []) => {
  if (!scope) return null
  const levels = toAccessLevels(scope)
  const resolved = levels.length ? levels : fallbackLevels
  return {
    access_levels: [...resolved],
    department_ids: resolved.includes('department') ? [...(scope.department_ids || [])] : [],
    user_uids: resolved.includes('user') ? [...(scope.user_uids || [])] : []
  }
}
