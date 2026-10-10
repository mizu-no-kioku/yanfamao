from yuxi.knowledge.utils.security import redact_sensitive_params


def test_redact_sensitive_params_removes_nested_credentials():
    params = {
        "safe": "value",
        "token": "top-level",
        "extractor_options": {
            "model_spec": "provider:model",
            "model_params": {
                "api_key": "nested-secret",
                "extra_body": [{"password": "hidden", "mode": "safe"}],
            },
        },
    }

    assert redact_sensitive_params(params) == {
        "safe": "value",
        "extractor_options": {
            "model_spec": "provider:model",
            "model_params": {"extra_body": [{"mode": "safe"}]},
        },
    }
