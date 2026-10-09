"""受控业务数据库工具。"""

import os

_REQUIRED_ENV = (
    "YUXI_BUSINESS_DB_HOST",
    "YUXI_BUSINESS_DB_USER",
    "YUXI_BUSINESS_DB_PASSWORD",
    "YUXI_BUSINESS_DB_NAME",
    "YUXI_BUSINESS_DB_IDENTITIES_JSON",
)

if all(os.getenv(name, "").strip() for name in _REQUIRED_ENV):
    from .tools import describe_business_database_table, list_business_database_tables, query_business_database

    __all__ = [
        "describe_business_database_table",
        "list_business_database_tables",
        "query_business_database",
    ]
else:
    __all__: list[str] = []
