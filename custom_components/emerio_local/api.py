"""Local transport for the Emerio PAC-127111.1."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime, timezone
import logging
from typing import Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
import tinytuya

from .const import PROTOCOL_VERSION
from .mapping import EmerioState, KNOWN_DPS, apply_dps

_LOGGER = logging.getLogger(__name__)

_STATUS_TIMEOUT = 3.0
_POLL_INTERVAL = 30.0


class EmerioCommunicationError(HomeAssistantError):
    """Raised when a local command cannot be placed on the wire."""


class InvalidLocalKey(ValueError):
    """Raised when a local key cannot be used by Tuya protocol 3.4."""


class EmerioDevice:
    """Keep one local session open and merge real Tuya updates into HA state."""

    def __init__(
        self,
        hass: HomeAssistant,
        name: str,
        host: str,
        device_id: str,
        local_key: str,
    ) -> None:
        validate_local_key(local_key)
        self.hass = hass
        self.name = name
        self.host = host
        self.device_id = device_id
        self._local_key = local_key
        self.state = EmerioState()

        self.command_reachable = False
        self.monitor_connected = False
        self.last_command: dict[str, Any] | None = None
        self.last_command_at: datetime | None = None
        self.last_status_at: datetime | None = None
        self.last_connect_at: datetime | None = None
        self.last_disconnect_at: datetime | None = None
        self.last_device_dps: dict[str, Any] | None = None
        self.last_error: str | None = None

        self._lock = asyncio.Lock()
        self._monitor_lock = asyncio.Lock()
        self._listeners: set[Callable[[], None]] = set()
        self._status_waiters: set[asyncio.Future[None]] = set()
        self._pending_dps: dict[int, Any] = {}
        self._monitor: Any | None = None
        self._monitor_device: Any | None = None
        self._monitor_registered = False
        self._poll_task: asyncio.Task[None] | None = None
        self._status_task: asyncio.Task[None] | None = None
        self._stopping = False

    @callback
    def add_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
        self._listeners.add(listener)

        @callback
        def remove_listener() -> None:
            self._listeners.discard(listener)

        return remove_listener

    @callback
    def _notify(self) -> None:
        for listener in tuple(self._listeners):
            listener()

    async def async_start(self) -> None:
        """Open a persistent Tuya 3.4 session and start listening for pushes."""

        self._stopping = False
        await self._async_start_monitor_transport()
        self._poll_task = self.hass.async_create_background_task(
            self._async_poll_loop(), f"emerio_local_poll_{self.device_id}"
        )

    async def _async_start_monitor_transport(self) -> bool:
        """Create the persistent monitor without changing the poll task."""

        self._monitor_device = self._new_tuya_device(timeout=3.0, persist=True)
        self._monitor_device.set_dpsUsed({str(dp): None for dp in KNOWN_DPS})
        self._monitor = tinytuya.Monitor(
            on_status=self._monitor_status_callback,
            on_connect=self._monitor_connect_callback,
            on_disconnect=self._monitor_disconnect_callback,
            heartbeat_interval=12,
            auto_reconnect=True,
            reconnect_backoff=3.0,
        )

        try:
            await self.hass.async_add_executor_job(self._start_monitor_sync)
        except Exception as err:
            # A one-shot write fallback remains usable even if Monitor cannot start.
            self._monitor_registered = False
            self.monitor_connected = False
            self.command_reachable = False
            self.last_error = f"Dauerverbindung: {err}"
            _LOGGER.warning(
                "Persistent session to %s failed; command fallback remains available: %s",
                self.host,
                err,
            )
            await self.hass.async_add_executor_job(self._stop_monitor_sync)
            self._monitor = None
            self._monitor_device = None
            self._notify()
            return False

        self._monitor_registered = True
        self.monitor_connected = True
        self.command_reachable = True
        self.last_connect_at = datetime.now(timezone.utc)
        self.last_error = None
        self._notify()

        # The first query lets TinyTuya detect device22. The second query uses
        # that detected format; UPDATEDPS is an additional firmware-specific path.
        self._schedule_status_sequence()
        return True

    async def async_stop(self) -> None:
        """Stop background work and close the persistent socket."""

        self._stopping = True
        tasks = [task for task in (self._status_task, self._poll_task) if task]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._status_task = None
        self._poll_task = None

        for waiter in tuple(self._status_waiters):
            if not waiter.done():
                waiter.cancel()
        self._status_waiters.clear()

        if self._monitor is not None:
            await self.hass.async_add_executor_job(self._stop_monitor_sync)
        self._monitor = None
        self._monitor_device = None
        self._monitor_registered = False
        self.monitor_connected = False
        self.command_reachable = False

    async def async_write_dps(self, dps: dict[int, Any]) -> None:
        """Send DPs and let a real device frame replace the temporary state."""

        if not dps:
            return

        async with self._lock:
            payload = {str(dp): value for dp, value in dps.items()}
            try:
                if self._monitor_registered:
                    if self.monitor_connected:
                        self._queue_monitor_command("set_multiple_values", payload)
                        self.command_reachable = True
                        self.last_error = None
                    else:
                        # Monitor owns the device connection while reconnecting.
                        # Keep the latest value per DP and send it on reconnect.
                        self._pending_dps.update(dps)
                        self.command_reachable = False
                        self.last_error = "Verbindung wird aufgebaut; Befehl vorgemerkt"
                else:
                    await self.hass.async_add_executor_job(self._write_dps_sync, dps)
                    self.command_reachable = True
                    self.last_error = None
            except Exception as err:
                self.command_reachable = False
                self.last_error = str(err)
                self._notify()
                raise EmerioCommunicationError(
                    f"Lokaler Befehl an {self.host} fehlgeschlagen: {err}"
                ) from err

            self.last_command = payload
            self.last_command_at = datetime.now(timezone.utc)
            apply_dps(self.state, dps, "optimistic")
            self._notify()

        if self.monitor_connected:
            # Capture the control response first, then actively ask for a report.
            self._schedule_status_sequence(initial_delay=0.2)

    async def async_refresh(self) -> None:
        """Request real DPs and wait briefly for the passive monitor callback."""

        if not self._monitor_registered or not self.monitor_connected:
            try:
                dps = await self.hass.async_add_executor_job(self._refresh_sync)
            except Exception as err:
                self.last_error = f"Statusabfrage: {err}"
                self._notify()
                raise EmerioCommunicationError(self.last_error) from err
            self._apply_device_dps(dps)
            return

        waiter = self.hass.loop.create_future()
        self._status_waiters.add(waiter)
        self._schedule_status_sequence(force=True)
        try:
            await asyncio.wait_for(waiter, timeout=_STATUS_TIMEOUT)
        except TimeoutError:
            await self._async_recover_monitor_status()
        finally:
            self._status_waiters.discard(waiter)

    async def _async_recover_monitor_status(self) -> None:
        """Recover a stale monitor after the appliance lost mains power."""

        async with self._monitor_lock:
            status_task = self._status_task
            if status_task is not None and not status_task.done():
                status_task.cancel()
                await asyncio.gather(status_task, return_exceptions=True)
            self._status_task = None

            if self._monitor is not None:
                await self.hass.async_add_executor_job(self._stop_monitor_sync)
            self._monitor = None
            self._monitor_device = None
            self._monitor_registered = False
            self.monitor_connected = False
            self.command_reachable = False

            try:
                dps = await self.hass.async_add_executor_job(self._refresh_sync)
            except Exception as err:
                self.last_error = f"Statusabfrage: {err}"
                self._notify()
                await self._async_start_monitor_transport()
                raise EmerioCommunicationError(self.last_error) from err

            self._apply_device_dps(dps)
            await self._async_start_monitor_transport()

    async def async_wait_for_device_dp(
        self, dp: int, expected: Any, timeout: float = 2.0
    ) -> bool:
        """Wait until the device, not the optimistic state, confirms a DP value."""

        def is_confirmed() -> bool:
            return (
                self.last_device_dps is not None
                and self.last_device_dps.get(str(dp)) == expected
                and dp in self.state.confirmed_dps
            )

        if is_confirmed():
            return True

        confirmed = asyncio.Event()

        @callback
        def handle_update() -> None:
            if is_confirmed():
                confirmed.set()

        remove_listener = self.add_listener(handle_update)
        try:
            if is_confirmed():
                return True
            await asyncio.wait_for(confirmed.wait(), timeout=timeout)
        except TimeoutError:
            return False
        finally:
            remove_listener()
        return True

    def _start_monitor_sync(self) -> None:
        if self._monitor is None or self._monitor_device is None:
            raise EmerioCommunicationError("Monitor wurde nicht initialisiert")
        handle = self._monitor.add(self._monitor_device)
        if isinstance(handle, str):
            raise EmerioCommunicationError(handle)
        self._monitor.start()

    def _stop_monitor_sync(self) -> None:
        if self._monitor is not None:
            self._monitor.stop()
        elif self._monitor_device is not None:
            self._monitor_device.close()

    def _monitor_status_callback(self, _device: Any, result: Any) -> None:
        if not self._stopping:
            self.hass.loop.call_soon_threadsafe(self._handle_monitor_status, result)

    def _monitor_connect_callback(self, _device: Any, error: Any) -> None:
        if not self._stopping:
            self.hass.loop.call_soon_threadsafe(self._handle_monitor_connect, error)

    def _monitor_disconnect_callback(self, _device: Any, error: Any) -> None:
        if not self._stopping:
            self.hass.loop.call_soon_threadsafe(self._handle_monitor_disconnect, error)

    @callback
    def _handle_monitor_status(self, result: Any) -> None:
        dps = _extract_dps(result)
        if dps:
            self._apply_device_dps(dps)

    @callback
    def _handle_monitor_connect(self, error: Any) -> None:
        self.monitor_connected = error is None
        self.command_reachable = error is None
        if error is None:
            self.last_connect_at = datetime.now(timezone.utc)
            if self.last_error and (
                self.last_error.startswith("Verbindung")
                or self.last_error.startswith("Dauerverbindung")
            ):
                self.last_error = None
            if self._pending_dps:
                pending = dict(self._pending_dps)
                self._pending_dps.clear()
                self._queue_monitor_command(
                    "set_multiple_values",
                    {str(dp): value for dp, value in pending.items()},
                )
            self._schedule_status_sequence(initial_delay=0.1)
        else:
            self.last_error = f"Verbindungsaufbau: {error}"
        self._notify()

    @callback
    def _handle_monitor_disconnect(self, error: Any) -> None:
        self.monitor_connected = False
        self.command_reachable = False
        self.last_disconnect_at = datetime.now(timezone.utc)
        self.last_error = f"Verbindung getrennt: {error}"
        self._notify()

    @callback
    def _apply_device_dps(self, dps: dict[int | str, Any]) -> None:
        if not apply_dps(self.state, dps, "device"):
            return
        if self.last_device_dps is None:
            self.last_device_dps = {}
        self.last_device_dps.update({str(dp): value for dp, value in dps.items()})
        self.last_status_at = datetime.now(timezone.utc)
        self.command_reachable = True
        self.last_error = None
        for waiter in tuple(self._status_waiters):
            if not waiter.done():
                waiter.set_result(None)
        self._notify()

    def _queue_monitor_command(self, method: str, *args: Any) -> None:
        if self._monitor is None or self._monitor_device is None:
            raise EmerioCommunicationError("Dauerverbindung ist nicht verfügbar")
        self._monitor.command(self._monitor_device, method, *args)

    @callback
    def _schedule_status_sequence(
        self, *, initial_delay: float = 0.0, force: bool = False
    ) -> None:
        if not self.monitor_connected or self._stopping:
            return
        if self._status_task is not None and not self._status_task.done():
            if not force:
                return
            self._status_task.cancel()
        self._status_task = self.hass.async_create_task(
            self._async_status_sequence(initial_delay),
            f"emerio_local_status_{self.device_id}",
        )

    async def _async_status_sequence(self, initial_delay: float) -> None:
        try:
            if initial_delay:
                await asyncio.sleep(initial_delay)
            if not self.monitor_connected:
                return
            self._queue_monitor_command("status")
            await asyncio.sleep(0.4)
            if not self.monitor_connected:
                return
            self._queue_monitor_command("status")
            await asyncio.sleep(0.4)
            if self.monitor_connected:
                self._queue_monitor_command("updatedps", list(KNOWN_DPS))
        except asyncio.CancelledError:
            raise
        except Exception as err:  # pragma: no cover - defensive background boundary
            _LOGGER.debug("Unable to request Emerio status: %s", err)

    async def _async_poll_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(_POLL_INTERVAL)
                if self._monitor_registered:
                    self._schedule_status_sequence()
                    continue

                async with self._monitor_lock:
                    if self._monitor_registered:
                        continue
                    try:
                        dps = await self.hass.async_add_executor_job(
                            self._refresh_sync
                        )
                    except Exception as err:
                        self.command_reachable = False
                        self.last_error = f"Statusabfrage: {err}"
                        self._notify()
                        continue
                    self._apply_device_dps(dps)
                    await self._async_start_monitor_transport()
        except asyncio.CancelledError:
            raise

    def _write_dps_sync(self, dps: dict[int, Any]) -> None:
        device = self._new_tuya_device(timeout=3.0, persist=False)
        try:
            payload = {str(dp): value for dp, value in dps.items()}
            result = device.set_multiple_values(payload, nowait=True)
            _raise_for_tuya_error(result)
        finally:
            device.close()

    def _refresh_sync(self) -> dict[str, Any]:
        device = self._new_tuya_device(timeout=2.0, persist=False)
        try:
            device.set_dpsUsed({str(dp): None for dp in KNOWN_DPS})
            result = device.status()
            _raise_for_tuya_error(result)
            dps = _extract_dps(result)
            if not dps:
                raise EmerioCommunicationError("Gerät lieferte keine Datenpunkte")
            return dps
        finally:
            device.close()

    def _new_tuya_device(self, timeout: float, persist: bool):
        device = tinytuya.OutletDevice(
            self.device_id,
            self.host,
            self._local_key,
            dev_type="default",
            connection_timeout=timeout,
            version=PROTOCOL_VERSION,
            persist=persist,
            connection_retry_limit=1,
            connection_retry_delay=0,
        )
        device.set_socketTimeout(timeout)
        device.set_socketRetryLimit(1)
        device.set_socketRetryDelay(0)
        device.set_retry(False)
        device.set_sendWait(0.05)
        return device


def validate_local_key(local_key: str) -> None:
    """Validate TinyTuya's 16-byte protocol 3.4 key requirement."""

    try:
        encoded = local_key.encode("latin1")
    except UnicodeEncodeError as err:
        raise InvalidLocalKey("Der Local Key enthält ungültige Zeichen") from err
    if len(encoded) != 16:
        raise InvalidLocalKey("Der Local Key muss genau 16 Byte lang sein")


def probe_device_sync(host: str, device_id: str, local_key: str) -> None:
    """Test TCP plus the 3.4 session-key handshake without changing a DP."""

    validate_local_key(local_key)
    device = tinytuya.OutletDevice(
        device_id,
        host,
        local_key,
        dev_type="default",
        connection_timeout=3,
        version=PROTOCOL_VERSION,
        persist=False,
        connection_retry_limit=1,
        connection_retry_delay=0,
    )
    try:
        device.set_socketRetryLimit(1)
        device.set_socketRetryDelay(0)
        device.set_retry(False)
        result = device.heartbeat(nowait=True)
        _raise_for_tuya_error(result)
    finally:
        device.close()


def _raise_for_tuya_error(result: Any) -> None:
    if not isinstance(result, dict):
        return
    err = result.get("Err")
    message = result.get("Error")
    if err not in (None, 0, "0", "") or message:
        raise EmerioCommunicationError(
            f"TinyTuya Fehler {err if err is not None else '?'}: {message or result}"
        )


def _extract_dps(result: Any) -> dict[str, Any]:
    if not isinstance(result, dict):
        return {}
    if isinstance(result.get("dps"), dict):
        return result["dps"]
    data = result.get("data")
    if isinstance(data, dict) and isinstance(data.get("dps"), dict):
        return data["dps"]
    return {}
