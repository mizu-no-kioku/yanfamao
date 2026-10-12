"""防误连守卫的负向证据：三种错误目标都必须在**任何 DDL 之前**被拒。

守卫的存在理由：这组租户夹具会执行实例级全局 DDL（`CREATE ROLE yuxi_app`、`ALTER ROLE`、
`GRANT ... ON SCHEMA public`），跑在开发实例上会改到共享角色与公共 schema——隔离 schema
只隔离数据，隔离不了实例级对象。

三条负向：① 目标实例不对（标记不符）② 库名不对 ③ 标记来源不是命令行（可能被运行期改过）。
另有一条正向：正确的测试实例必须被接受。

本文件不需要数据库夹具，只需要容器内的 `POSTGRES_TEST_URL`。
"""

from __future__ import annotations

import pytest

import _tenant_schema_test_db as guard

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


@pytest.fixture(scope="session", autouse=True)
def ensure_live_api_schema():
    """本文件不碰数据库，也不需要运行中的 API。"""


@pytest.fixture(scope="session", autouse=True)
def cleanup_test_knowledge_resources():
    yield


@pytest.fixture(scope="session", autouse=True)
def cleanup_test_sandboxes():
    yield


async def test_the_dedicated_test_instance_is_accepted():
    """正向：正确的测试实例必须通过——否则守卫就是"全都拒绝"的假绿。"""

    await guard.assert_target_is_the_test_instance(force=True)


async def test_a_database_without_the_instance_marker_is_rejected(monkeypatch):
    """① 目标实例不对：库名对、但实例没有测试标记。

    这一条覆盖的正是"开发实例上恰好也有一个叫 yuxi_test 的库"——专用库名挡不住它，
    目标比较也挡不住**连接别名**（hosts/DNS/端口转发指向同一实例）。
    """

    monkeypatch.setenv("POSTGRES_TEST_CLUSTER_MARKER", "not-this-instance")
    with pytest.raises(RuntimeError, match="测试实例标记不符"):
        await guard.assert_target_is_the_test_instance(force=True)


async def test_a_wrong_database_name_is_rejected(monkeypatch):
    """② 库名不对：连得上，但不是专用测试库。"""

    monkeypatch.setenv("POSTGRES_TEST_DB", "yuxi")
    with pytest.raises(RuntimeError, match="期望专用测试库"):
        await guard.assert_target_is_the_test_instance(force=True)


async def test_a_marker_not_set_on_the_command_line_is_rejected(monkeypatch):
    """③ 标记来源不是命令行。

    标记是 superuser 可在运行期改的（`SET` / `ALTER SYSTEM`），所以只认 `source='command line'`。
    这里替换读取函数来造出"配置来源"的标记——那是我们要拒绝的形态。
    """

    async def fake_marker(_connection):
        return (guard.expected_cluster_marker(), "configuration file")

    monkeypatch.setattr(guard, "_read_cluster_marker", fake_marker)
    with pytest.raises(RuntimeError, match="来源不是命令行"):
        await guard.assert_target_is_the_test_instance(force=True)


async def test_the_failure_messages_never_leak_the_password(monkeypatch):
    """报错会进日志与 CI 输出，绝不能带密码。"""

    monkeypatch.setenv("POSTGRES_TEST_CLUSTER_MARKER", "not-this-instance")
    with pytest.raises(RuntimeError) as excinfo:
        await guard.assert_target_is_the_test_instance(force=True)

    message = str(excinfo.value)
    assert "postgres:postgres" not in message
    assert "@" not in message
