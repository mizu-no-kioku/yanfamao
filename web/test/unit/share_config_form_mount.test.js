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

// 每个用例各自建一个 vite server（SFC 编译结果按 server 缓存），用完必须 close。
const createFormHarness = async () => {
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

  const { default: ShareConfigForm } = await server.ssrLoadModule(
    'virtual:share-config-form-mount-test'
  )

  return { ShareConfigForm, close: () => server.close() }
}

// 卡片是渲染成普通对象节点的：Vue 把 `@click="toggleAccessLevel(...)"` 编译成 props.onClick。
const cardByTitle = (host, title) =>
  find(host, (node) => node.props.role === 'radio' && textContent(node).includes(title))

const clickCard = async (host, title) => {
  const card = cardByTitle(host, title)
  assert.ok(card, `未找到「${title}」卡片`)
  card.props.onClick()
  await nextTick()
}

// 共享范围开关未被解析时渲染成 a-switch 元素，@change 编译成 props.onChange。
const toggleSwitch = async (host, index, enabled = true) => {
  const node = findAll(host, (item) => item.type === 'a-switch')[index]
  assert.ok(node, `未找到第 ${index} 个共享范围开关`)
  node.props.onChange(enabled)
  await nextTick()
  await nextTick()
}

const mountForm = async (ShareConfigForm, modelValue, overrides = {}) => {
  const updates = []
  const host = makeNode('root')
  const app = renderer.createApp(() =>
    h(ShareConfigForm, {
      modelValue,
      autoSelectUserDept: true,
      requireReadScope: true,
      allowedAccessLevels: ['global', 'department', 'user'],
      'onUpdate:modelValue': (value) => updates.push(value),
      ...overrides
    })
  )
  app.mount(host)
  await nextTick()
  await nextTick()
  return { app, host, updates }
}

// 直接以 ShareConfigForm 为根组件挂载，好拿到它 defineExpose 出来的 validate()。
const mountFormInstance = async (ShareConfigForm, modelValue, overrides = {}) => {
  const updates = []
  const host = makeNode('root')
  const app = renderer.createApp(ShareConfigForm, {
    modelValue,
    autoSelectUserDept: true,
    requireReadScope: true,
    allowedAccessLevels: ['global', 'department', 'user'],
    'onUpdate:modelValue': (value) => updates.push(value),
    ...overrides
  })
  const instance = app.mount(host)
  await nextTick()
  await nextTick()
  return { app, host, updates, instance }
}

const selectedCard = (host) =>
  find(host, (node) => node.props.role === 'radio' && node.props['aria-checked'] === true)

test('未交互的默认共享范围只驱动显示，挂载时 0 次 emit', async () => {
  const harness = await createFormHarness()
  const { ShareConfigForm } = harness

  let app
  try {
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

    // 回归对照：缺 read_scope 的知识库不该被改写成全局可见。旧行为是回落 global 并
    // emit 一次；新行为是不 emit，交给 validate() 挡住保存。
    const missingReadScope = await mountForm(ShareConfigForm, { version: 2, read_scope: null, manage_scope: null })
    app = missingReadScope.app
    assert.deepEqual(missingReadScope.updates, [])
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
    await harness.close()
    delete globalThis.window
    delete globalThis.__shareConfigFormUser
  }
})

test('切到「指定人」再切回「部门共享」，部门选择不丢', async () => {
  const harness = await createFormHarness()
  try {
    const { app, host, updates } = await mountForm(harness.ShareConfigForm, {
      version: 2,
      read_scope: { access_levels: ['department'], department_ids: [2], user_uids: [] },
      manage_scope: null
    })

    await clickCard(host, '指定人') // 并集：加上「指定人」档，部门名单不能被清掉
    await clickCard(host, '部门共享') // 取消「部门共享」档
    await clickCard(host, '部门共享') // 再选回来，原部门名单应仍在

    const emitted = updates[updates.length - 1].read_scope
    assert.deepEqual(emitted.access_levels, ['department', 'user'])
    assert.deepEqual(emitted.department_ids, [2])
    app.unmount()
  } finally {
    await harness.close()
  }
})

test('部门 + 指定人可以同时选中并保存', async () => {
  const harness = await createFormHarness()
  try {
    const { app, host, updates } = await mountForm(harness.ShareConfigForm, {
      version: 2,
      read_scope: { access_levels: ['department'], department_ids: [2], user_uids: [] },
      manage_scope: null
    })

    await clickCard(host, '指定人')

    const emitted = updates[updates.length - 1].read_scope
    assert.deepEqual(emitted.access_levels, ['department', 'user'])
    assert.deepEqual(emitted.department_ids, [2])
    assert.deepEqual(emitted.user_uids, ['creator-uid'])
    app.unmount()
  } finally {
    await harness.close()
  }
})

test('最后一档不能被取消，选全局会清空两份名单', async () => {
  const harness = await createFormHarness()
  try {
    const { app, host, updates } = await mountForm(harness.ShareConfigForm, {
      version: 2,
      read_scope: { access_levels: ['user'], department_ids: [], user_uids: ['creator-uid'] },
      manage_scope: null
    })

    await clickCard(host, '指定人') // 已是唯一一档：点击应不生效——无变更即无 emit
    assert.deepEqual(updates, [])

    await clickCard(host, '全局共享')
    assert.deepEqual(updates[updates.length - 1].read_scope, {
      access_levels: ['global'],
      department_ids: [],
      user_uids: []
    })
    app.unmount()
  } finally {
    await harness.close()
  }
})

test('拿不到档位的范围既不落库也不被默认成全局', async () => {
  const harness = await createFormHarness()
  try {
    // 老 bundle 或缓存响应可能只带 department_ids/user_uids。这里钉住的是：表单不会为
    // 缺档位的形状凭空发明一档（旧实现只在 read_scope 整个缺失时才回落 global 并 emit，
    // 那条路径由上面「回归对照」的既有用例覆盖）。
    const { app, updates } = await mountForm(harness.ShareConfigForm, {
      version: 2,
      read_scope: { department_ids: [], user_uids: [] },
      manage_scope: null
    })

    assert.deepEqual(updates, [])
    app.unmount()
  } finally {
    await harness.close()
  }
})

test('读取范围非全局时，「共享管理权限」开关能打开且立刻是合法范围', async () => {
  const harness = await createFormHarness()
  try {
    const { app, host, updates } = await mountForm(harness.ShareConfigForm, {
      version: 2,
      read_scope: { access_levels: ['department'], department_ids: [2], user_uids: [] },
      manage_scope: null
    })

    // requireReadScope 为真时读取范围的开关不渲染，因此第 0 个开关就是「共享管理权限」。
    await toggleSwitch(host, 0)

    const emitted = updates[updates.length - 1].manage_scope
    assert.ok(emitted, '管理范围应被建立，开关不能是死开关')
    assert.ok(emitted.access_levels.length, '管理范围必须至少有一档')
    assert.ok(
      emitted.department_ids.length || emitted.user_uids.length,
      '管理范围必须带名单：含某档却不带名单会被后端拒绝'
    )
    app.unmount()
  } finally {
    await harness.close()
  }
})

test('非管理员关掉再打开读取范围，开关不会变成死开关', async () => {
  const harness = await createFormHarness()
  try {
    const { app, host, updates } = await mountForm(
      harness.ShareConfigForm,
      {
        version: 2,
        read_scope: { access_levels: ['user'], department_ids: [], user_uids: ['creator-uid'] },
        manage_scope: null
      },
      // requireReadScope 为假，读取范围的开关才渲染；第 0 个开关即读取范围。
      { allowedAccessLevels: ['user'], requireReadScope: false }
    )

    await toggleSwitch(host, 0, false)
    await toggleSwitch(host, 0, true)

    const emitted = updates[updates.length - 1].read_scope
    assert.deepEqual(emitted.access_levels, ['user'])
    assert.deepEqual(emitted.user_uids, ['creator-uid'])
    app.unmount()
  } finally {
    await harness.close()
  }
})

test('取消一档会清掉该档名单，不产出后端拒绝的形状', async () => {
  const harness = await createFormHarness()
  try {
    const { app, host, updates } = await mountForm(harness.ShareConfigForm, {
      version: 2,
      read_scope: { access_levels: ['department'], department_ids: [2], user_uids: [] },
      manage_scope: null
    })

    await clickCard(host, '指定人') // 并集：加上「指定人」档
    await clickCard(host, '部门共享') // 取消「部门共享」档

    const emitted = updates[updates.length - 1].read_scope
    assert.deepEqual(emitted.access_levels, ['user'])
    assert.deepEqual(emitted.department_ids, [], '未声明部门档时不能残留部门名单')
    app.unmount()
  } finally {
    await harness.close()
  }
})

test('requireReadScope 而响应里没有可用的 read_scope 时，validate() 说非法', async () => {
  const harness = await createFormHarness()
  try {
    const { app, instance } = await mountFormInstance(harness.ShareConfigForm, {
      version: 2,
      read_scope: null,
      manage_scope: null
    })

    assert.deepEqual(instance.validate(), {
      valid: false,
      message: '读取权限至少选择一种共享方式'
    })
    app.unmount()
  } finally {
    await harness.close()
  }
})
