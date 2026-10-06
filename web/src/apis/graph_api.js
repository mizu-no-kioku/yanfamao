import { apiGet, apiPost } from './base'

export const graphApi = {
  getGraphs: async () => {
    return await apiGet('/api/graph/list', {}, true)
  },

  querySubgraph: async (payload) => {
    if (!payload?.kb_id) {
      throw new Error('kb_id is required')
    }

    return await apiPost('/api/graph/subgraph', payload, {}, true)
  },

  getFilterOptions: async (kbId) => {
    if (!kbId) {
      throw new Error('kb_id is required')
    }

    const queryParams = new URLSearchParams({ kb_id: kbId })
    return await apiGet(`/api/graph/filter-options?${queryParams.toString()}`, {}, true)
  },

  searchEntities: async ({ kbId, q = '', limit = 20 } = {}) => {
    if (!kbId) {
      throw new Error('kb_id is required')
    }

    const queryParams = new URLSearchParams({ kb_id: kbId, q, limit: String(limit) })
    return await apiGet(`/api/graph/entities?${queryParams.toString()}`, {}, true)
  },

  getStats: async (kb_id) => {
    if (!kb_id) {
      throw new Error('kb_id is required')
    }

    const queryParams = new URLSearchParams({ kb_id })
    return await apiGet(`/api/graph/stats?${queryParams.toString()}`, {}, true)
  },

  getLabels: async (kb_id) => {
    if (!kb_id) {
      throw new Error('kb_id is required')
    }

    const queryParams = new URLSearchParams({ kb_id })
    return await apiGet(`/api/graph/labels?${queryParams.toString()}`, {}, true)
  }
}

export const unifiedApi = graphApi
