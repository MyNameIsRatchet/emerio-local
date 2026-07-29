"""Local transport for the Emerio PAC-127111.1."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

import tinytuya
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError

from .const import PROTOCOL_VERSION
from .mapping import KNOWN_DPS, EmerioState, apply_dps

_LOGGER = logging.getLogger(__name__)

_STATUS_TIMEOUT = 12.0
_POLL_INTERVAL = 30.0
_POST_COMMAND_STATUS_DELAY = 5.0
_STATUS_REQUEST_SPACING = 1.5

_DEVICE_TYPE_DEFAULT = "default"
_DEVICE_TYPE_22 = "device22"
_RETRYABLE_STATUS_ERROR_CODES = frozenset({"902", "908", "no_dps"})


class EmerioCommunicationError(HomeAssistantError):
    """Raised when a local command cannot be placed on the wire."""

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.code = code


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
        self.device_type = _preferred_device_type(device_id)

        self._lock = asyncio.Lock()
        self._monitor_lock = asyncio.Lock()
        self._listeners: set[Callable[[], None]] = set()
        self._status_waiters: set[asyncio.Future[None]] = set()
        self._pending_dps: dict[int, Any] = {}
        self._pending_confirmations: dict[int, Any] = {}
        self._monitor: Any | None = None
        self._monitor_device: Any | None = None
        self._monitor_handle: Any | None = None
        self._monitor_registered = False
        self._poll_task: asyncio.Task[None] | None = None
        self._status_task: asyncio.Task[None] | None = None
        self._initial_status_probe_attempted = False
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

        monitor_device = self._new_tuya_device(timeout=3.0, persist=True)
        monitor_device.set_dpsUsed({str(dp): None for dp in KNOWN_DPS})
        monitor = tinytuya.Monitor(
            on_status=self._monitor_status_callback,
            on_connect=self._monitor_connect_callback,
            on_disconnect=self._monitor_disconnect_callback,
            heartbeat_interval=12,
            auto_reconnect=True,
            reconnect_backoff=3.0,
        )
        self._monitor = monitor
        self._monitor_device = monitor_device
        self._monitor_handle = None

        try:
            handle = await self.hass.async_add_executor_job(
                self._start_monitor_sync, monitor, monitor_device
            )
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
            if self._monitor is monitor:
                self._monitor = None
                self._monitor_device = None
                self._monitor_handle = None
            await self.hass.async_add_executor_job(
                self._stop_monitor_sync, monitor, monitor_device
            )
            self._notify()
            return False

        if self._monitor is not monitor:
            await self.hass.async_add_executor_job(
                self._stop_monitor_sync, monitor, monitor_device
            )
            return False

        self._monitor_handle = handle
        self._monitor_registered = True
        self.monitor_connected = True
        self.command_reachable = True
        self.last_connect_at = datetime.now(timezone.utc)
        self.last_error = None
        self._notify()

        # The known 22-character Emerio ID starts directly in TinyTuya's device22
        # mode. A one-shot refresh still retries the alternate format when needed.
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

        monitor = self._monitor
        monitor_device = self._monitor_device
        self._monitor = None
        self._monitor_device = None
        self._monitor_handle = None
        self._monitor_registered = False
        self.monitor_connected = False
        self.command_reachable = False
        if monitor is not None or monitor_device is not None:
            await self.hass.async_add_executor_job(
                self._stop_monitor_sync, monitor, monitor_device
            )

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
            self._pending_confirmations.update(dps)
            apply_dps(self.state, dps, "optimistic")
            self._notify()

        if self.monitor_connected:
            # This firmware emits its previous state briefly after accepting a
            # command. Give it time to settle before requesting fresh reports.
            self._schedule_status_sequence(initial_delay=_POST_COMMAND_STATUS_DELAY)

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

            monitor = self._monitor
            monitor_device = self._monitor_device
            self._monitor = None
            self._monitor_device = None
            self._monitor_handle = None
            self._monitor_registered = False
            self.monitor_connected = False
            self.command_reachable = False
            if monitor is not None or monitor_device is not None:
                await self.hass.async_add_executor_job(
                    self._stop_monitor_sync, monitor, monitor_device
                )

            try:
                dps = await self.hass.async_add_executor_job(self._refresh_sync)
            except Exception as err:
                error_message = f"Statusabfrage: {err}"
                self.last_error = error_message
                self._notify()
                await self._async_start_monitor_transport()
                # Starting a fresh monitor clears last_error. Preserve the actual
                # status failure for diagnostics and for the button service call.
                self.last_error = error_message
                self._notify()
                raise EmerioCommunicationError(
                    error_message,
                    code=getattr(err, "code", None),
                ) from err

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

    def _start_monitor_sync(self, monitor: Any, monitor_device: Any) -> Any:
        if monitor is None or monitor_device is None:
            raise EmerioCommunicationError("Monitor wurde nicht initialisiert")
        handle = monitor.add(monitor_device)
        if isinstance(handle, str):
            raise EmerioCommunicationError(handle)
        monitor.start()
        return handle

    def _stop_monitor_sync(
        self,
        monitor: Any | None,
        monitor_device: Any | None,
    ) -> None:
        if monitor is not None:
            monitor.stop()
        elif monitor_device is not None:
            monitor_device.close()

    def _monitor_status_callback(self, device: Any, result: Any) -> None:
        if not self._stopping and device is self._monitor_device:
            self.hass.loop.call_soon_threadsafe(self._handle_monitor_status, result)

    def _monitor_connect_callback(self, device: Any, error: Any) -> None:
        if not self._stopping and device is self._monitor_device:
            self.hass.loop.call_soon_threadsafe(self._handle_monitor_connect, error)

    def _monitor_disconnect_callback(self, device: Any, error: Any) -> None:
        if not self._stopping and device is self._monitor_device:
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
            if self._monitor_registered:
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
        accepted_dps: dict[int | str, Any] = {}
        for raw_dp, value in dps.items():
            try:
                dp = int(raw_dp)
            except (TypeError, ValueError):
                accepted_dps[raw_dp] = value
                continue

            if dp not in self._pending_confirmations:
                accepted_dps[raw_dp] = value
                continue

            expected = self._pending_confirmations[dp]
            if value == expected:
                self._pending_confirmations.pop(dp, None)
                accepted_dps[raw_dp] = value
                continue

            # The PAC-127111.1 executes commands but can keep returning its
            # pre-command value indefinitely. Retain the honest optimistic
            # state until the same DP is confirmed instead of rolling the UI
            # back to a value that is demonstrably no longer active.
            _LOGGER.debug(
                "Ignoring stale DP %s=%r while waiting for commanded %r",
                dp,
                value,
                expected,
            )

        if not apply_dps(self.state, accepted_dps, "device"):
            return
        if self.last_device_dps is None:
            self.last_device_dps = {}
        self.last_device_dps.update(
            {str(dp): value for dp, value in accepted_dps.items()}
        )
        self.last_status_at = datetime.now(timezone.utc)
        self.command_reachable = True
        self.last_error = None
        for waiter in tuple(self._status_waiters):
            if not waiter.done():
                waiter.set_result(None)
        self._notify()

    def _queue_monitor_command(self, method: str, *args: Any) -> None:
        handle = self._monitor_handle
        if handle is None:
            raise EmerioCommunicationError("Dauerverbindung ist nicht verfügbar")
        getattr(handle, method)(*args)

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
            await asyncio.sleep(_STATUS_REQUEST_SPACING)
            if not self.monitor_connected:
                return
            self._queue_monitor_command("status")
            await asyncio.sleep(_STATUS_REQUEST_SPACING)
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
                    if (
                        self.last_status_at is None
                        and not self._initial_status_probe_attempted
                    ):
                        self._initial_status_probe_attempted = True
                        try:
                            await self._async_recover_monitor_status()
                        except EmerioCommunicationError:
                            # The precise failure remains in last_error. Keep the
                            # optimistic control path alive and continue polling.
                            pass
                        continue
                    self._schedule_status_sequence()
                    continue

                async with self._monitor_lock:
                    if self._monitor_registered:
                        continue
                    try:
                        dps = await self.hass.async_add_executor_job(self._refresh_sync)
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
        errors: list[tuple[str, EmerioCommunicationError]] = []
        candidates = _device_type_candidates(self.device_type)
        for index, device_type in enumerate(candidates):
            device = self._new_tuya_device(
                timeout=2.0,
                persist=False,
                device_type=device_type,
            )
            try:
                device.set_dpsUsed({str(dp): None for dp in KNOWN_DPS})
                result = device.status()
                _raise_for_tuya_error(result)
                dps = _extract_dps(result)
                if not dps:
                    raise EmerioCommunicationError(
                        "Gerät lieferte keine Datenpunkte",
                        code="no_dps",
                    )
            except EmerioCommunicationError as err:
                errors.append((device_type, err))
                has_alternate = index < len(candidates) - 1
                if not has_alternate or err.code not in _RETRYABLE_STATUS_ERROR_CODES:
                    raise
                _LOGGER.debug(
                    "Status request using Tuya type %s failed (%s); trying %s",
                    device_type,
                    err,
                    candidates[index + 1],
                )
            else:
                if self.device_type != device_type:
                    _LOGGER.info(
                        "Using Tuya device type %s for %s status reports",
                        device_type,
                        self.host,
                    )
                self.device_type = device_type
                return dps
            finally:
                device.close()

        device_type, error = errors[-1]
        raise EmerioCommunicationError(
            f"Statusabfrage mit Tuya-Typ {device_type} fehlgeschlagen: {error}",
            code=error.code,
        ) from error

    def _new_tuya_device(
        self,
        timeout: float,
        persist: bool,
        device_type: str | None = None,
    ):
        device = tinytuya.OutletDevice(
            self.device_id,
            self.host,
            self._local_key,
            dev_type=device_type or self.device_type,
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


def _preferred_device_type(device_id: str) -> str:
    """Select the Tuya query format most likely used by this device."""

    return _DEVICE_TYPE_22 if len(device_id) == 22 else _DEVICE_TYPE_DEFAULT


def _device_type_candidates(preferred: str) -> tuple[str, str]:
    """Return the preferred Tuya type followed by its safe fallback."""

    alternate = (
        _DEVICE_TYPE_DEFAULT if preferred == _DEVICE_TYPE_22 else _DEVICE_TYPE_22
    )
    return preferred, alternate


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
        dev_type=_preferred_device_type(device_id),
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
        code = str(err) if err not in (None, "") else None
        raise EmerioCommunicationError(
            f"TinyTuya Fehler {err if err is not None else '?'}: {message or result}",
            code=code,
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
