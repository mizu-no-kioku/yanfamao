"""出厂品牌配置的中文名与拉丁名断言。"""

from __future__ import annotations

from pathlib import Path

import pytest

from server.routers.system_router import load_info_config

pytestmark = pytest.mark.unit

BACKEND_ROOT = Path(__file__).resolve().parents[3]
TEMPLATE_PATH = BACKEND_ROOT / "package" / "yuxi" / "config" / "static" / "info.template.yaml"


async def test_shipped_template_uses_new_brand_names(monkeypatch):
    """出厂模板解析出研发猫 / YanfaMao，且不再包含旧中文名。"""
    monkeypatch.delenv("YUXI_BRAND_FILE_PATH", raising=False)
    monkeypatch.chdir(BACKEND_ROOT)

    config = await load_info_config()

    assert config["organization"]["name"] == "研发猫"
    assert config["branding"]["name"] == "YanfaMao"
    assert "语析" not in TEMPLATE_PATH.read_text(encoding="utf-8")


async def test_override_file_takes_precedence_over_template(monkeypatch, tmp_path):
    """负向案例：环境变量指向的文件内容决定结果，证明上面的断言读的是文件而非常量。"""
    override = tmp_path / "info.local.yaml"
    override.write_text(
        'organization:\n  name: "语析"\nbranding:\n  name: "Yuxi"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("YUXI_BRAND_FILE_PATH", str(override))

    config = await load_info_config()

    assert config["organization"]["name"] == "语析"
    assert config["branding"]["name"] == "Yuxi"
