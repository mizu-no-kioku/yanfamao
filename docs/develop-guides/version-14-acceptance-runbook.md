# 版本 14 验收运行手册（源码运行环境隔离）

本文只讲**怎么跑**，不讲要验什么。验收项清单见
`docs/develop-guides/decisions/proposed/2026-10-10-multi-tenant-isolation.md` 的验收矩阵与
`.superpowers/sdd/2026-10-10-multi-tenant-foundation-plan/progress.md` 的最新一轮裁决。

## 为什么必须隔离源码运行环境（实测，不是顾虑）

`docker-compose.yml` 里两个长驻服务都对 `backend/package` 做了**热监看**：

```yaml
api:    command: uv run --no-sync --no-dev uvicorn server.main:app ... --reload \
                 --reload-dir /app/server --reload-dir /app/package
worker: command: watchfiles --filter python "python -m server.worker_main" /app/server /app/package
```

也就是说：**在主工作树里改 `backend/package` 下任何一个字节，运行中的开发 api/worker 会立刻自动重载**——
不是"下次重启才出问题"。而版本 14 的接线必然要改 `BUSINESS_SCHEMA_VERSION`，开发库却仍停在版本 13，
重载即触发启动闸门 `require_current_schema()`：**开发 API 与 worker 会拒绝启动**。

第四轮已经撞过一次同类事故（当时为把容器救回来，处置是**退回版本常量**而不是跑迁移）。

**禁止的规避方式**（审查明确要求）：临时关闭版本检查、静默回退、把闸门改成 warn。这些会让
"未迁移的代码跑在未迁移的库上"从一次硬失败变成一次静默的不一致。

## 机制：独立 worktree + 独立 compose 项目

- **独立 worktree**：版本 14 的源码在另一个目录，开发 api/worker 挂载的主工作树保持版本 13 代码。
- **独立 compose 项目**（`-p yuxi-v14`）：自己的一整套 postgres / redis / postgres-test / redis-test，
  与开发栈的实例互不可见；主机端口改道，避免与开发栈抢端口。

当前 worktree：`../yuxi-v14-wt`，分支 `part2/version-14-migration`。

## 一次性准备

```bash
cd /d/AI/python/Yuxi-main/Yuxi-main
git worktree add -b part2/version-14-migration ../yuxi-v14-wt HEAD
cp .env ../yuxi-v14-wt/.env          # .env 被 gitignore，不进库
```

## 每轮怎么跑

```bash
cd /d/AI/python/Yuxi-main/yuxi-v14-wt

# 端口改道：开发栈已占用 5432/6379/6433/6479
export MSYS_NO_PATHCONV=1
export YUXI_POSTGRES_PORT=15432 YUXI_REDIS_PORT=16379 \
       YUXI_POSTGRES_TEST_PORT=16433 YUXI_REDIS_TEST_PORT=16479

# 起隔离实例（首次）
docker compose -p yuxi-v14 --profile test-db up -d postgres redis postgres-test redis-test

# 每轮开跑前重建测试实例（tmpfs，重建即空库；迁移轮次断言"库是空的"）
docker compose -p yuxi-v14 --profile test-db up -d --force-recreate postgres-test redis-test

docker compose -p yuxi-v14 --profile test-db run --rm --no-deps api \
  uv run --no-sync pytest test/integration/storage_migration -q -p no:cacheprovider
```

`run --no-deps` 是有意的：`api` 的 `depends_on` 会拖起整套依赖，而验收只需要网络里那四个实例。

## 一次性前置：给隔离项目的"开发目录"建立锚点

`test_development_directories_are_not_written` 的判据是**开发 Skill 目录里已存在迁移标记**
（`.legacy-migration-complete`）——标记就是"这棵树跑过迁移"的锚点，没有它这条断言没有意义，
用例会**正确地**报 `这条断言没有锚点` 而失败。

隔离项目的卷是全新的，因此要先把这件事做一次（与开发栈"容器跑过迁移"等价）：

```bash
docker compose -p yuxi-v14 run --rm storage-migrator
```

它会把隔离项目的 `postgres` 迁到当前版本，并在隔离项目自己的 `skill-sources` 里写下标记。
**这一步只动 `-p yuxi-v14` 的资源**，开发栈与开发库不受影响。

## 跑完怎么收

```bash
docker compose -p yuxi-v14 down --remove-orphans     # 保留 worktree 源码，只回收容器与网络
```

worktree 本身按需保留或 `git worktree remove ../yuxi-v14-wt`（未提交改动会被拒绝删除，是安全的）。

## 边界（不要夸大）

- 隔离的是**源码运行环境**与**数据库/Redis 实例**。Neo4j / Milvus / MinIO / 沙箱 **没有**隔离，
  与主工作树的测试轮次相同（见 `backend/test/integration/storage_migration/README.md` 的「已知限制」）。
- 本手册**不构成**任何验收结论，也不意味着版本 14 的代码已经接入。
