import { h } from 'vue'
import { Database, DatabaseZap } from '@lucide/vue'

const ICON_BASE = 'https://registry.npmmirror.com/@lobehub/icons-static-svg/latest/files/icons'

const createBrandIcon = (url) => {
  const Icon = ({ size = 20 }) =>
    h('img', { src: url, style: { width: size + 'px', height: size + 'px' } })
  Icon.inheritAttrs = false
  return Icon
}

export const brandIcons = {
  dify: createBrandIcon(`${ICON_BASE}/dify-color.svg`),
  notion: createBrandIcon(`${ICON_BASE}/notion.svg`)
}

export const getKbTypeLabel = (type) => {
  const normalized = String(type || '').toLowerCase()
  const labels = {
    milvus: '研发猫',
    dify: 'Dify',
    notion: 'Notion'
  }
  return labels[normalized] || type
}

export const getKbTypeIcon = (type) => {
  const icons = {
    milvus: DatabaseZap,
    dify: brandIcons.dify,
    notion: brandIcons.notion
  }
  return icons[type] || Database
}

export const getKbTypeColor = (type) => {
  const colors = {
    milvus: 'blue',
    dify: 'gold',
    notion: 'purple'
  }
  return colors[type] || 'blue'
}

const READ_ONLY_KB_TYPES = new Set(['dify', 'notion'])

export const isReadOnlyDatabase = (database, kbTypes = {}) => {
  const kbType = (
    typeof database === 'string' ? database : database?.kb_type || 'milvus'
  ).toLowerCase()

  if (database?.supports_documents !== undefined) {
    return database.supports_documents === false
  }
  if (kbTypes[kbType]?.supports_documents !== undefined) {
    return kbTypes[kbType].supports_documents === false
  }
  return READ_ONLY_KB_TYPES.has(kbType)
}

/**
 * 判断知识库是否是当前用户自己的个人库。
 *
 * 判据是归属而不是管理权限：管理员对所有知识库的 `can_manage` 都是 true，
 * 用它当归属依据会把别人的个人库错认成自己建的。
 */
export const isOwnPersonalDatabase = (database, uid) =>
  database?.scope === 'personal' && database?.created_by === uid

export const kbUtils = {
  getKbTypeLabel,
  getKbTypeIcon,
  getKbTypeColor,
  isReadOnlyDatabase,
  isOwnPersonalDatabase
}
