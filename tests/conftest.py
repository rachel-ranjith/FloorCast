import pytest

from config.settings import get_settings


@pytest.fixture
def settings():
    get_settings.cache_clear()
    return get_settings()
