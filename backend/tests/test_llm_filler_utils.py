import pytest
from unittest.mock import AsyncMock, MagicMock
from backend.app.browser_automation.forms.llm_filler import _read_field_value, _values_match
from backend.app.browser_automation.forms.models import FormField
from module2.run_scrape import get_api_base_url


def test_values_match():
    assert _values_match("Fluent (Professional Working Proficiency).", "Fluent (Professional Working Proficiency).")
    assert _values_match("Fluent (Professional Working Proficiency).", "Fluent")
    assert _values_match("San Francisco", "San Francisco, CA")
    assert not _values_match("Fluent (Professional Working Proficiency).", "402090")


def test_get_api_base_url_default(monkeypatch):
    monkeypatch.delenv("M1_API_BASE_URL", raising=False)
    monkeypatch.delenv("API_BASE_URL", raising=False)
    monkeypatch.delenv("API_URL", raising=False)
    assert get_api_base_url() == "http://localhost:8002"


def test_get_api_base_url_rewrites_8000(monkeypatch):
    monkeypatch.setenv("M1_API_BASE_URL", "http://localhost:8000/api")
    assert get_api_base_url() == "http://localhost:8002"


@pytest.mark.asyncio
async def test_read_field_value_native_select():
    mock_page = MagicMock()
    mock_locator = AsyncMock()
    mock_locator.count.return_value = 1
    mock_locator.evaluate.return_value = "Fluent (Professional Working Proficiency)."
    mock_page.locator.return_value.first = mock_locator

    field = FormField(
        selector="select#english_fluency",
        label="*How would you describe your English Fluency?",
        field_type="select",
        required=True,
        custom_widget=False
    )

    val = await _read_field_value(mock_page, field)
    assert val == "Fluent (Professional Working Proficiency)."
