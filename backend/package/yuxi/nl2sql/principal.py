"""调用主体的窄接口：解析器只认它，不认 Yuxi 内部结构。"""

from pydantic import BaseModel, ConfigDict, PositiveInt


class BusinessPrincipal(BaseModel):
    """业务侧身份。tenant_id 为 None 表示由业务库推导；跨租户时必须显式给出。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    user_id: PositiveInt
    tenant_id: PositiveInt | None = None
