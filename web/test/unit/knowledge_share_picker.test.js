import assert from 'node:assert/strict'
import test from 'node:test'

import { createRenderer, h, nextTick } from 'vue'
import { createServer } from 'vite'
import { compileScript, parse } from 'vue/compiler-sfc'
import { readFileSync } from 'node:fs'

import { createDerivedShareConfig } from '../../src/utils/databaseCreateForm.js'

// 复用 share_config_form_mount.test.js 的 SFC 编译 + 自定义渲染器范式。
// ShareConfigForm 依赖全局注册的 ant-design-vue 与两个 API 模块；这里把 API 与 store
// 换成桩，ant 组件未解析时会渲染成同名元素，足以断言卡片文本与渲染出的选项数。

const readSource = (relativePath) => readFileSync(new URL(relativePath, import.meta.url), 'utf8')

const makeNode = (type) => {
  const node = { type, children: [], props: {}, style: {}, parent: null, text: '' }
  node.focus = () => {
    node.focused = true
  }
  return node
}

const renderer = createRenderer({
  createElement: makeNode,
  createText(text) {
    return { ...makeNode('text'), text }
  },
  createComment(text) {
    return { ...makeNode('comment'), text }
  },
  insert(child, parent, anchor = null) {
    child.parent = parent
    const index = anchor ? parent.children.indexOf(anchor) : -1
    if (index < 0) parent.children.push(child)
    else parent.children.splice(index, 0, child)
  },
  remove(child) {
    const index = child.parent?.children.indexOf(child) ?? -1
    if (index >= 0) child.parent.children.splice(index, 1)
  },
  setText(node, text) {
    node.text = text
  },
  setElementText(node, text) {
    node.text = text
    node.children = []
  },
  parentNode: (node) => node.parent,
  nextSibling: (node) => node.parent?.children[node.parent.children.indexOf(node) + 1] || null,
  patchProp(node, key, _oldValue, value) {
    node.props[key] = value
  }
})

const findAll = (node, predicate) => {
  const matches = []
  if (predicate(node)) matches.push(node)
  for (const child of node.children || []) matches.push(...findAll(child, predicate))
  return matches
}

const textContent = (node) =>
  [node.text, ...(node.children || []).map((child) => textContent(child))].join('')

const stubModules = {
  '@/stores/user': 'export const useUserStore = () => globalThis.__sharePickerUser',
  '@/apis/auth_api': 'export const authApi = { getUserAccessOptions: async () => [] }',
  '@/apis/department_api':
    'export const departmentApi = { getDepartments: async () => { globalThis.__departmentLoadCalls = (globalThis.__departmentLoadCalls || 0) + 1; return { departments: [] } } }'
}

const shareCards = (host) => findAll(host, (node) => node.props.role === 'radio')

const mountForm = async (ShareConfigForm, modelValue, allowedAccessLevels) => {
  const host = makeNode('root')
  const app = renderer.createApp(() =>
    h(ShareConfigForm, {
      modelValue,
      autoSelectUserDept: true,
      requireReadScope: true,
      allowedAccessLevels
    })
  )
  app.mount(host)
  await nextTick()
  await nextTick()
  return { app, host }
}

test('详情页与 API 传输层：分享选择器不再要求管理员', () => {
  const detailSource = readSource('../../src/views/DataBaseInfoView.vue')
  const authSource = readSource('../../src/apis/auth_api.js')

  // 个人库只提供「指定人」；共享库保持三档。后端对个人库的非 user 级 read_scope fail-closed，
  // 展示全局/部门只会让人以为分享成功而实际无人可见。
  assert.match(
    detailSource,
    /:allowed-access-levels="\s*database\.scope === 'personal'\s*\?\s*\['user'\]\s*:\s*\['global', 'department', 'user'\]\s*"/
  )
  // 用户目录对普通登录用户开放，走普通传输层而不是 apiAdminGet。
  assert.match(
    authSource,
    /async function getUserAccessOptions\(\) \{\s*return apiGet\('\/api\/auth\/users\/access-options'\)/
  )
  assert.doesNotMatch(
    authSource,
    /async function getUserAccessOptions\(\) \{\s*return apiAdminGet/
  )
})

test('个人库只渲染「指定人」且不请求部门列表；共享库仍有三档', async () => {
  globalThis.window = {
    getComputedStyle: () => ({
      lineHeight: '20',
      paddingTop: '0',
      paddingBottom: '0',
      borderTopWidth: '0',
      borderBottomWidth: '0'
    })
  }
  globalThis.__sharePickerUser = { departmentId: 3, uid: 'creator-uid', isAdmin: false }
  globalThis.__departmentLoadCalls = 0

  const server = await createServer({
    server: { middlewareMode: true, hmr: false },
    appType: 'custom',
    plugins: [
      {
        name: 'knowledge-share-picker-test',
        enforce: 'pre',
        resolveId(id) {
          if (id === 'virtual:knowledge-share-picker-test') return `\0${id}`
          for (const specifier of Object.keys(stubModules)) {
            const resolved = specifier.replace(/^@/, '')
            if (id === resolved || id.endsWith(resolved) || id.endsWith(`${resolved}.js`)) {
              return `\0stub:${specifier}`
            }
          }
          return undefined
        },
        load(id) {
          if (id === '\0virtual:knowledge-share-picker-test') {
            const source = readFileSync(
              new URL('../../src/components/ShareConfigForm.vue', import.meta.url),
              'utf8'
            )
            const { descriptor } = parse(source)
            return compileScript(descriptor, {
              id: 'knowledge-share-picker-test',
              inlineTemplate: true
            }).content
          }
          if (id.startsWith('\0stub:')) return stubModules[id.slice('\0stub:'.length)]
          return undefined
        }
      }
    ]
  })

  let app
  try {
    const { default: ShareConfigForm } = await server.ssrLoadModule(
      'virtual:knowledge-share-picker-test'
    )

    // 个人库：详情页传 ['user'] → 只有「指定人」，且不发起部门列表请求。
    const personal = await mountForm(
      ShareConfigForm,
      createDerivedShareConfig({ scope: 'personal', departmentId: 3, uid: 'creator-uid' }),
      ['user']
    )
    app = personal.app
    const personalCards = shareCards(personal.host)
    assert.equal(personalCards.length, 1, '个人库应只渲染一张分享卡片')
    assert.match(textContent(personalCards[0]), /指定人/)
    assert.doesNotMatch(textContent(personalCards[0]), /全局共享|部门共享/)
    assert.equal(globalThis.__departmentLoadCalls, 0, '不允许部门级时不应请求部门列表')
    app.unmount()

    // 共享库：详情页传三档 → 三张卡片，且仍会加载部门列表。
    const shared = await mountForm(
      ShareConfigForm,
      createDerivedShareConfig({ scope: 'shared', departmentId: 3, uid: 'creator-uid' }),
      ['global', 'department', 'user']
    )
    app = shared.app
    const sharedCards = shareCards(shared.host)
    assert.equal(sharedCards.length, 3, '共享库应渲染全部三张分享卡片')
    const sharedText = sharedCards.map(textContent).join('|')
    assert.match(sharedText, /全局共享/)
    assert.match(sharedText, /部门共享/)
    assert.match(sharedText, /指定人/)
    assert.equal(globalThis.__departmentLoadCalls, 1, '允许部门级时应加载部门列表')
    app.unmount()
    app = undefined
  } finally {
    app?.unmount()
    await server.close()
    delete globalThis.window
    delete globalThis.__sharePickerUser
    delete globalThis.__departmentLoadCalls
  }
})
