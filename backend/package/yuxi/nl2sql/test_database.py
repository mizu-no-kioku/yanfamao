"""隔离测试库的连接配置。

只认专用环境变量；缺失即跳过，绝不回退到业务库配置——撤权验收会写数据，
回退会把写操作打到生产业务库上。
"""

import json
import os

import pytest
from pydantic import BaseModel, ConfigDict, SecretStr, ValidationError

TEST_CONFIG_ENV = "YUXI_NL2SQL_TEST_DATABASE_CONFIG"


class TestDatabaseConfig(BaseModel):
    """隔离测试库：应用账号只读、控制账号可写。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    host: str
    port: int
    database: str
    app_user: str
    app_password: SecretStr
    control_user: str
    control_password: SecretStr


def load_test_database_config() -> TestDatabaseConfig | None:
    raw = os.environ.get(TEST_CONFIG_ENV)
    if not raw:
        return None
    try:
        return TestDatabaseConfig.model_validate(json.loads(raw))
    except (json.JSONDecodeError, ValidationError):
        return None


def require_test_database() -> TestDatabaseConfig:
    config = load_test_database_config()
    if config is None:
        pytest.skip(f"未配置 {TEST_CONFIG_ENV}，跳过需要隔离测试库的用例")
    return config
