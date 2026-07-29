from __future__ import annotations

import importlib
import sys
import types
from pathlib import Path

import pytest


@pytest.fixture
def api_module(monkeypatch):
    """Load the transport module with minimal Home Assistant test doubles."""

    repository = Path(__file__).parents[1]

    homeassistant = types.ModuleType("homeassistant")
    homeassistant.__path__ = []
    core = types.ModuleType("homeassistant.core")
    core.HomeAssistant = object
    core.callback = lambda function: function
    exceptions = types.ModuleType("homeassistant.exceptions")
    exceptions.HomeAssistantError = Exception
    tinytuya = types.ModuleType("tinytuya")

    custom_components = types.ModuleType("custom_components")
    custom_components.__path__ = [str(repository / "custom_components")]
    integration = types.ModuleType("custom_components.emerio_local")
    integration.__path__ = [str(repository / "custom_components" / "emerio_local")]
    const = types.ModuleType("custom_components.emerio_local.const")
    const.PROTOCOL_VERSION = 3.4

    modules = {
        "homeassistant": homeassistant,
        "homeassistant.core": core,
        "homeassistant.exceptions": exceptions,
        "tinytuya": tinytuya,
        "custom_components": custom_components,
        "custom_components.emerio_local": integration,
        "custom_components.emerio_local.const": const,
    }
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)

    sys.modules.pop("custom_components.emerio_local.mapping", None)
    sys.modules.pop("custom_components.emerio_local.api", None)
    return importlib.import_module("custom_components.emerio_local.api")
