"""知识库配置的敏感信息处理工具。"""

from typing import Any

_SENSITIVE_PARAMETER_MARKERS = ("token", "secret", "password", "api_key")


def redact_sensitive_params(params: dict[str, Any]) -> dict[str, Any]:
    """递归移除知识库类型参数中的敏感凭据。"""
    return _redact_value(params)


def _redact_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _redact_value(item)
            for key, item in value.items()
            if not any(marker in str(key).lower() for marker in _SENSITIVE_PARAMETER_MARKERS)
        }
    if isinstance(value, list):
        return [_redact_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_value(item) for item in value)
    return value
