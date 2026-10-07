import assert from 'node:assert/strict'
import test from 'node:test'

import { createRenderer, defineComponent, h, nextTick, ref } from 'vue'
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
    'export const departmentApi = { getDepartments: async () => { globalThis.__departmentLoadCalls = (globalThis.__departmentLoadCalls || 0) + 1; if (globalThis.__departmentLoadFailures > 0) { globalThis.__departmentLoadFailures -= 1; throw new Error("department request failed") }; return { departments: [{ id: 3, name: "研发部" }] } } }'
}

const flush = async () => {
  await nextTick()
  await nextTick()
  await new Promise((resolve) => setTimeout(resolve, 0))
  await nextTick()
}

const shareCards = (host) => findAll(host, (node) => node.props.role === 'radio')
const checkedCard = (host) => shareCards(host).find((card) => card.props['aria-checked'] === true)

// 包装组件：把 allowedAccessLevels 表达为一个可变的 ref / 每次渲染新建的数组字面量，
// 以复现父组件重渲染时 prop 身份的变化。
const makeWrapper = (ShareConfigForm, modelValue, allowedAccessLevels, tick) =>
  defineComponent({
    setup() {
      return () =>
        h(ShareConfigForm, {
          modelValue,
          autoSelectUserDept: true,
          requireReadScope: true,
          'data-render': tick.value,
          allowedAccessLevels: allowedAccessLevels.value
        })
    }
  })

const mountWrapper = async (ShareConfigForm, modelValue, allowedAccessLevels) => {
  const tick = ref(0)
  const host = makeNode('root')
  const app = renderer.createApp(makeWrapper(ShareConfigForm, modelValue, allowedAccessLevels, tick))
  app.mount(host)
  await nextTick()
  await nextTick()
  return { app, host, tick }
}

test('详情页与 API 传输层：分享选择器不再要求管理员', () => {
  const detailSource = readSource('../../src/views/DataBaseInfoView.vue')
  const authSource = readSource('../../src/apis/auth_api.js')

  // 用户目录对普通登录用户开放，走普通传输层而不是 apiAdminGet。
  assert.match(
    authSource,
    /async function getUserAccessOptions\(\) \{\s*return apiGet\('\/api\/auth\/users\/access-options'\)/
  )
  assert.doesNotMatch(
    authSource,
    /async function getUserAccessOptions\(\) \{\s*return apiAdminGet/
  )

  // 个人库只提供「指定人」，共享库三档。两个数组是 setup 作用域常量，computed 只负责选择：
  // 自动刷新会不断重新赋值 store.database，若数组字面量写在 computed 里，每次重算都会产出新
  // 数组、prop 身份抖动，进而让 ShareConfigForm 重新派生 scopes 并丢弃未保存的本地选择。
  assert.match(detailSource, /const SHARE_ACCESS_LEVELS_PERSONAL = \['user'\]/)
  assert.match(detailSource, /const SHARE_ACCESS_LEVELS_SHARED = \['global', 'department', 'user'\]/)
  assert.match(detailSource, /:allowed-access-levels="shareAllowedAccessLevels"/)
  assert.doesNotMatch(detailSource, /:allowed-access-levels="\s*database\.scope/)

  const computedBlock = detailSource.match(
    /const shareAllowedAccessLevels = computed\(\(\) =>[\s\S]*?\n\)/
  )
  assert.ok(computedBlock, '应存在 shareAllowedAccessLevels computed')
  assert.match(computedBlock[0], /database\.value\.scope === 'personal'/)
  assert.match(computedBlock[0], /SHARE_ACCESS_LEVELS_PERSONAL/)
  assert.match(computedBlock[0], /SHARE_ACCESS_LEVELS_SHARED/)
  assert.doesNotMatch(
    computedBlock[0],
    /\['/,
    'computed 内不得出现数组字面量，否则 scope 未变时也会产出新数组'
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

  const server = await createServer(serverOptions())
  let app
  try {
    const { default: ShareConfigForm } = await server.ssrLoadModule(
      'virtual:knowledge-share-picker-test'
    )

    // 个人库：详情页传 ['user'] → 只有「指定人」，且不发起部门列表请求。
    const personal = await mountWrapper(
      ShareConfigForm,
      createDerivedShareConfig({ scope: 'personal', departmentId: 3, uid: 'creator-uid' }),
      ref(['user'])
    )
    app = personal.app
    const personalCards = shareCards(personal.host)
    assert.equal(personalCards.length, 1, '个人库应只渲染一张分享卡片')
    assert.match(textContent(personalCards[0]), /指定人/)
    assert.doesNotMatch(textContent(personalCards[0]), /全局共享|部门共享/)
    assert.equal(globalThis.__departmentLoadCalls, 0, '不允许部门级时不应请求部门列表')
    app.unmount()

    // 共享库：详情页传三档 → 三张卡片，且仍会加载部门列表。
    globalThis.__departmentLoadCalls = 0
    const shared = await mountWrapper(
      ShareConfigForm,
      createDerivedShareConfig({ scope: 'shared', departmentId: 3, uid: 'creator-uid' }),
      ref(['global', 'department', 'user'])
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

test('挂载后放宽为共享库时补拉部门列表，而不是永久空选项', async () => {
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

  const server = await createServer(serverOptions())
  const allowed = ref(['user'])
  let app
  try {
    const { default: ShareConfigForm } = await server.ssrLoadModule(
      'virtual:knowledge-share-picker-test'
    )

    const mounted = await mountWrapper(
      ShareConfigForm,
      createDerivedShareConfig({ scope: 'personal', departmentId: 3, uid: 'creator-uid' }),
      allowed
    )
    app = mounted.app
    assert.equal(globalThis.__departmentLoadCalls, 0)
    assert.equal(shareCards(mounted.host).length, 1)

    // 同一挂载实例上放宽为共享库：部门列表必须补拉，否则部门下拉是「暂无可选项」。
    allowed.value = ['global', 'department', 'user']
    await nextTick()
    await nextTick()

    assert.equal(shareCards(mounted.host).length, 3)
    assert.equal(globalThis.__departmentLoadCalls, 1, '放宽后应补拉一次部门列表')
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

test('部门列表加载失败后，切到「部门共享」会重试', async () => {
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
  globalThis.__departmentLoadFailures = 1

  const server = await createServer(serverOptions())
  let app
  try {
    const { default: ShareConfigForm } = await server.ssrLoadModule(
      'virtual:knowledge-share-picker-test'
    )

    // 共享库 + 无部门 → 初始为「指定人」，三档都可选。
    const mounted = await mountWrapper(
      ShareConfigForm,
      createDerivedShareConfig({ scope: 'shared', departmentId: null, uid: 'creator-uid' }),
      ref(['global', 'department', 'user'])
    )
    app = mounted.app
    await flush()
    assert.equal(globalThis.__departmentLoadCalls, 1, '首次加载应已发起')

    // 生产里可达的重试时机：用户切到「部门共享」。首次失败后否则选择器永久为空、validate() 挡住保存。
    const departmentCard = shareCards(mounted.host).find((card) =>
      /部门共享/.test(textContent(card))
    )
    departmentCard.props.onClick()
    await flush()
    assert.equal(globalThis.__departmentLoadCalls, 2, '切到部门共享时应重试')
    app.unmount()
    app = undefined
  } finally {
    app?.unmount()
    await server.close()
    delete globalThis.window
    delete globalThis.__sharePickerUser
    delete globalThis.__departmentLoadCalls
    delete globalThis.__departmentLoadFailures
  }
})

// 这条**刻画** ShareConfigForm 的行为：allowedAccessLevels 的身份变化会让它重新从 modelValue
// 派生 scopes、丢弃未保存的本地选择。它跑的是同一份组件代码，区分不了详情页修复前后，因此
// **不构成** #4 修复的证据；详情页那一侧的修复由上面「详情页与 API 传输层…」的源码断言证明。
test('（刻画）ShareConfigForm 对 allowedAccessLevels 的身份敏感', async () => {
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

  const server = await createServer(serverOptions())
  let app
  try {
    const { default: ShareConfigForm } = await server.ssrLoadModule(
      'virtual:knowledge-share-picker-test'
    )
    const modelValue = createDerivedShareConfig({ scope: 'shared', departmentId: 3, uid: 'creator-uid' })

    const selectUserCard = async (host) => {
      const userCard = shareCards(host).find((card) => /指定人/.test(textContent(card)))
      userCard.props.onClick()
      await nextTick()
    }

    // 稳定引用（详情页用 computed 传的就是它）：父组件重渲染不重新派生 scopes。
    const stableLevels = ['global', 'department', 'user']
    const stable = await mountWrapper(ShareConfigForm, modelValue, ref(stableLevels))
    app = stable.app
    assert.match(textContent(checkedCard(stable.host)), /部门共享/)
    await selectUserCard(stable.host)
    assert.match(textContent(checkedCard(stable.host)), /指定人/)
    stable.tick.value += 1
    await nextTick()
    await nextTick()
    assert.match(
      textContent(checkedCard(stable.host)),
      /指定人/,
      '稳定的 prop 引用不应让父组件重渲染丢弃本地选择'
    )
    app.unmount()

    // 对照组（证明上一条不是空转）：每次渲染新建数组字面量 → 重新派生 → 选择被静默丢弃。
    const fresh = ref(['global', 'department', 'user'])
    const churn = await mountWrapper(ShareConfigForm, modelValue, {
      get value() {
        return fresh.value.slice()
      }
    })
    app = churn.app
    assert.match(textContent(checkedCard(churn.host)), /部门共享/)
    await selectUserCard(churn.host)
    assert.match(textContent(checkedCard(churn.host)), /指定人/)
    churn.tick.value += 1
    await nextTick()
    await nextTick()
    assert.match(
      textContent(checkedCard(churn.host)),
      /部门共享/,
      '身份变化的 prop 会让表单从 modelValue 重新派生并丢弃本地选择'
    )
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

function serverOptions() {
  return {
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
  }
}
