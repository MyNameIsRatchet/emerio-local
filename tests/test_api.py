from __future__ import annotations

import asyncio

import pytest


class FakeStatusDevice:
    def __init__(self, result):
        self.result = result
        self.requested_dps = None
        self.closed = False

    def set_dpsUsed(self, dps):
        self.requested_dps = dps

    def status(self):
        return self.result

    def close(self):
        self.closed = True


class InlineHass:
    async def async_add_executor_job(self, function, *args):
        return function(*args)


def _device(api_module, device_id="bf01a838bf807c0f39iavi"):
    return api_module.EmerioDevice(
        hass=InlineHass(),
        name="Emerio",
        host="192.0.2.10",
        device_id=device_id,
        local_key="0123456789abcdef",
    )


def test_22_character_ids_prefer_device22(api_module):
    assert api_module._preferred_device_type("a" * 22) == "device22"
    assert api_module._preferred_device_type("a" * 20) == "default"


def test_refresh_uses_device22_and_remembers_success(api_module):
    device = _device(api_module)
    created = []
    fake_devices = []

    def new_device(*, timeout, persist, device_type=None):
        created.append((timeout, persist, device_type))
        fake_device = FakeStatusDevice({"dps": {"3": 23}})
        fake_devices.append(fake_device)
        return fake_device

    device._new_tuya_device = new_device

    assert device._refresh_sync() == {"3": 23}
    assert created == [(2.0, False, "device22")]
    assert fake_devices[0].requested_dps == {"1": None, "2": None, "3": None}
    assert device.device_type == "device22"


def test_refresh_retries_alternate_type_after_timeout(api_module):
    device = _device(api_module)
    results = iter(
        [
            {"Err": "902", "Error": "Timeout Waiting for Device"},
            {"dps": {"3": 21}},
        ]
    )
    created = []

    def new_device(*, timeout, persist, device_type=None):
        created.append(device_type)
        return FakeStatusDevice(next(results))

    device._new_tuya_device = new_device

    assert device._refresh_sync() == {"3": 21}
    assert created == ["device22", "default"]
    assert device.device_type == "default"


def test_monitor_commands_use_registered_proxy(api_module):
    device = _device(api_module)
    calls = []

    class Handle:
        def status(self):
            calls.append("status")

    device._monitor_handle = Handle()
    device._queue_monitor_command("status")

    assert calls == ["status"]


def test_device22_status_groups_are_small_and_cover_known_dps(api_module):
    groups = api_module._status_request_groups("device22")

    assert groups[0] == (1, 2, 3)
    assert all(len(group) <= 3 for group in groups)
    assert set().union(*map(set, groups)) == set(api_module.KNOWN_DPS)
    assert all(1 in group for group in groups)


def test_monitor_cycles_device22_status_groups(api_module, monkeypatch):
    device = _device(api_module)
    calls = []

    class Handle:
        def set_dpsUsed(self, dps):
            calls.append(("set_dpsUsed", dps))

        def status(self):
            calls.append(("status",))

        def updatedps(self, dps):
            calls.append(("updatedps", dps))

    monkeypatch.setattr(api_module, "_STATUS_REQUEST_SPACING", 0)
    device._monitor_handle = Handle()
    device.monitor_connected = True

    asyncio.run(device._async_status_sequence(0))

    expected = []
    for group in api_module._DEVICE22_STATUS_GROUPS:
        expected.extend(
            [
                ("set_dpsUsed", {str(dp): None for dp in group}),
                ("status",),
            ]
        )
    expected.append(("updatedps", list(api_module.KNOWN_DPS)))
    assert calls == expected


def test_recovery_preserves_status_error_after_reconnect(api_module):
    device = _device(api_module)

    def failed_refresh():
        raise api_module.EmerioCommunicationError(
            "TinyTuya Fehler 902: Timeout Waiting for Device",
            code="902",
        )

    async def restarted_monitor():
        device.last_error = None
        device.monitor_connected = True
        return True

    device._refresh_sync = failed_refresh
    device._async_start_monitor_transport = restarted_monitor

    with pytest.raises(
        api_module.EmerioCommunicationError,
        match="TinyTuya Fehler 902",
    ) as raised:
        asyncio.run(device._async_recover_monitor_status())

    assert raised.value.code == "902"
    assert "TinyTuya Fehler 902" in device.last_error
