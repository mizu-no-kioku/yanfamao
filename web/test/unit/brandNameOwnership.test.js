import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

const readSource = (relativePath) => readFileSync(new URL(relativePath, import.meta.url), 'utf8')

test('产品名兜底与欢迎语使用研发猫，不残留旧品牌名', () => {
  const appLayout = readSource('../../src/layouts/AppLayout.vue')
  const loginView = readSource('../../src/views/LoginView.vue')
  const chat = readSource('../../src/components/AgentChatComponent.vue')
  const kbUtils = readSource('../../src/utils/kb_utils.js')
  const knowledgeGraph = readSource('../../src/components/KnowledgeGraphSection.vue')

  assert.doesNotMatch(appLayout, /\|\| 'Yuxi'/, 'AppLayout 品牌兜底不得回落旧名')
  assert.doesNotMatch(loginView, /\|\| 'Yuxi'/, 'LoginView 品牌兜底不得回落旧名')
  assert.doesNotMatch(chat, /'语析，/, '欢迎语不得残留旧中文名')
  assert.doesNotMatch(kbUtils, /milvus: 'Yuxi'/, '内置类型标签不得残留旧名')
  assert.doesNotMatch(knowledgeGraph, /只有 Yuxi 类型的知识库/, '图谱说明不得残留旧名')
})
