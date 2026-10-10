export function getShareConfigLabel(shareConfig) {
  const config = shareConfig || {}
  const readScope = config.version === 2 ? config.read_scope : config
  const manageScope = config.manage_scope
  if (config.version === 2 && !config.read_scope && !manageScope) return '仅所有者'
  const scopeLabel = (scope) => {
    if (!scope) return '无'
    if (scope.access_level === 'global') return '全局'
    if (scope.access_level === 'department') return `部门(${scope.department_ids?.length || 0})`
    return `用户(${scope.user_uids?.length || 0})`
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
  return (Array.isArray(raw) ? raw : []).filter((level) =>
    ['global', 'department', 'user'].includes(level)
  )
}

/** 是否全局共享；只按档位数组判定，并集不是全局。 */
export const isGlobalScope = (scope) => toAccessLevels(scope).includes('global')
