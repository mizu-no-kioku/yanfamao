import assert from 'node:assert/strict'
import test from 'node:test'

import { createRenderer, h, nextTick } from 'vue'
import { createServer } from 'vite'
import { compileScript, parse } from 'vue/compiler-sfc'
import { readFileSync } from 'node:fs'

import { createDerivedShareConfig } from '../../src/utils/databaseCreateForm.js'

// 复用 humanApprovalModal.test.js 的 SFC 编译 + 自定义渲染器范式：
// ShareConfigForm 依赖 ant-design-vue（全局注册）与两个 API 模块，这里把 API 与
// store 换成桩模块，ant 组件未解析时会被渲染成同名元素，足以断言 aria-checked / 文本。

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

const find = (node, predicate) => {
  if (predicate(node)) return node
  for (const child of node.children || []) {
    const match = find(child, predicate)
    if (match) return match
  }
  return undefined
}

const findAll = (node, predicate) => {
  const matches = []
  if (predicate(node)) matches.push(node)
  for (const child of node.children || []) matches.push(...findAll(child, predicate))
  return matches
}

const textContent = (node) =>
  [node.text, ...(node.children || []).map((child) => textContent(child))].join('')

const stubModules = {
  '@/stores/user': 'export const useUserStore = () => globalThis.__shareConfigFormUser',
  '@/apis/auth_api': 'export const authApi = { getUserAccessOptions: async () => [] }',
  '@/apis/department_api': 'export const departmentApi = { getDepartments: async () => ({ departments: [] }) }'
}

const mountForm = async (ShareConfigForm, modelValue) => {
  const updates = []
  const host = makeNode('root')
  const app = renderer.createApp(() =>
    h(ShareConfigForm, {
      modelValue,
      autoSelectUserDept: true,
      requireReadScope: true,
      allowedAccessLevels: ['global', 'department', 'user'],
      'onUpdate:modelValue': (value) => updates.push(value)
    })
  )
  app.mount(host)
  await nextTick()
  await nextTick()
  return { app, host, updates }
}

const selectedCard = (host) =>
  find(host, (node) => node.props.role === 'radio' && node.props['aria-checked'] === true)

test('未交互的默认共享范围只驱动显示，挂载时 0 次 emit', async () => {
  globalThis.window = { getComputedStyle: () => ({ lineHeight: '20', paddingTop: '0', paddingBottom: '0', borderTopWidth: '0', borderBottomWidth: '0' }) }
  globalThis.__shareConfigFormUser = { departmentId: 3, uid: 'creator-uid', isAdmin: true }

  const server = await createServer({
    server: { middlewareMode: true, hmr: false },
    appType: 'custom',
    plugins: [
      {
        name: 'share-config-form-mount-test',
        enforce: 'pre',
        resolveId(id) {
          if (id === 'virtual:share-config-form-mount-test') return `\0${id}`
          // Vite 已先把 `@` 别名解析成绝对路径，这里按已解析路径的结尾匹配桩模块。
          for (const specifier of Object.keys(stubModules)) {
            const resolved = specifier.replace(/^@/, '')
            if (id === resolved || id.endsWith(resolved) || id.endsWith(`${resolved}.js`)) {
              return `\0stub:${specifier}`
            }
          }
          return undefined
        },
        load(id) {
          if (id === '\0virtual:share-config-form-mount-test') {
            const source = readFileSync(
              new URL('../../src/components/ShareConfigForm.vue', import.meta.url),
              'utf8'
            )
            const { descriptor } = parse(source)
            return compileScript(descriptor, {
              id: 'share-config-form-mount-test',
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
      'virtual:share-config-form-mount-test'
    )

    // 共享库有部门：显示"部门共享"，且挂载时不能 emit（emit 会把派生权威搬到前端）。
    const sharedWithDepartment = await mountForm(
      ShareConfigForm,
      createDerivedShareConfig({ scope: 'shared', departmentId: 3, uid: 'creator-uid' })
    )
    app = sharedWithDepartment.app
    assert.deepEqual(sharedWithDepartment.updates, [])
    const departmentCard = selectedCard(sharedWithDepartment.host)
    assert.ok(departmentCard, '应有一张卡片处于选中态')
    assert.match(textContent(departmentCard), /部门共享/)
    app.unmount()

    // 共享库无部门：显示"指定人"（仅创建者），仍然 0 次 emit。
    const sharedNoDepartment = await mountForm(
      ShareConfigForm,
      createDerivedShareConfig({ scope: 'shared', departmentId: null, uid: 'creator-uid' })
    )
    app = sharedNoDepartment.app
    assert.deepEqual(sharedNoDepartment.updates, [])
    app.unmount()

    // 个人库：仍为"指定人"，0 次 emit。
    const personal = await mountForm(
      ShareConfigForm,
      createDerivedShareConfig({ scope: 'personal', departmentId: 3, uid: 'creator-uid' })
    )
    app = personal.app
    assert.deepEqual(personal.updates, [])
    app.unmount()
    app = undefined

    // 回归对照：若默认范围缺失 read_scope，表单会回落 global 并在挂载时 emit——
    // 这正是本轮风险点，用它证明上面的 0 次 emit 不是空转。
    const missingReadScope = await mountForm(ShareConfigForm, { version: 2, read_scope: null, manage_scope: null })
    app = missingReadScope.app
    assert.equal(missingReadScope.updates.length, 1)
    assert.equal(missingReadScope.updates[0].read_scope.access_level, 'global')
    app.unmount()
    app = undefined

    // 共享库场景不再预选"全局共享"。
    const sharedCards = await mountForm(
      ShareConfigForm,
      createDerivedShareConfig({ scope: 'shared', departmentId: 3, uid: 'creator-uid' })
    )
    app = sharedCards.app
    const activeCards = findAll(
      sharedCards.host,
      (node) => node.props.role === 'radio' && node.props['aria-checked'] === true
    )
    assert.equal(activeCards.length, 1)
    assert.doesNotMatch(textContent(activeCards[0]), /全局共享/)
  } finally {
    app?.unmount()
    await server.close()
    delete globalThis.window
    delete globalThis.__shareConfigFormUser
  }
})
