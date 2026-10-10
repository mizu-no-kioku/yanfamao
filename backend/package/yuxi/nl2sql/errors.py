"""可诊断的拒绝原因。每个拒绝都必须带码，便于审计与排障。"""

IDENTITY_USER_MISSING = "IDENTITY_USER_MISSING"
IDENTITY_USER_INACTIVE = "IDENTITY_USER_INACTIVE"
IDENTITY_USER_NO_TENANT = "IDENTITY_USER_NO_TENANT"
IDENTITY_TENANT_NOT_OWNED = "IDENTITY_TENANT_NOT_OWNED"
IDENTITY_STAFF_MISSING = "IDENTITY_STAFF_MISSING"
IDENTITY_STAFF_AMBIGUOUS = "IDENTITY_STAFF_AMBIGUOUS"
TENANT_AMBIGUOUS = "TENANT_AMBIGUOUS"
ROLE_MISSING = "ROLE_MISSING"
ROLE_INACTIVE = "ROLE_INACTIVE"
ROLE_CARDINALITY_INVALID = "ROLE_CARDINALITY_INVALID"
FUNCTION_GATE_DENIED = "FUNCTION_GATE_DENIED"
NO_PROJECT_ACCESS = "NO_PROJECT_ACCESS"


class PolicyDenial(Exception):
    """权限拒绝。调用方应把它作为最终结果，不得据此重试或换表。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"
