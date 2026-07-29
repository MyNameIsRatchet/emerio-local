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
from .mapping import DP_POWER, KNOWN_DPS, EmerioState, apply_dps

_LOGGER = logging.getLogger(__name__)

_STATUS_TIMEOUT = 12.0
_POLL_INTERVAL = 30.0
_BOOTSTRAP_RETRY_INTERVAL = 5.0
_BOOTSTRAP_CYCLE_BACKOFF = 300.0
_STATUS_CONNECTION_TIMEOUT = 5.0
_BOOTSTRAP_PROTOCOLS = (
    ("3.4", 3.4, False),
    ("3.3", 3.3, False),
    ("3.1", 3.1, False),
    ("3.2", 3.2, True),
    ("3.5", 3.5, False),
    ("3.22", 3.3, True),
)


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
        self.last_status_protocol: str | None = None
        self.last_error: str | None = None
        self.power_sensor_entity_id: str | None = None
        self.power_watts: float | None = None
        self.power_fallback_active = False
        self.compressor_active: bool | None = None
        self._power_on_threshold = 10.0
        self._compressor_threshold = 300.0

        self._lock = asyncio.Lock()
        self._monitor_lock = asyncio.Lock()
        self._listeners: set[Callable[[], None]] = set()
        self._status_waiters: set[asyncio.Future[None]] = set()
        self._pending_dps: dict[int, Any] = {}
        self._pending_confirmations: dict[int, Any] = {}
        self._monitor: Any | None = None
        self._monitor_device: Any | None = None
        self._monitor_registered = False
        self._status_requests_enabled = False
        self._passive_updatedps_sent = False
        self._bootstrap_protocol_index = 0
        self.bootstrap_cycle_exhausted = False
        self._active_protocol = PROTOCOL_VERSION
        self._active_device22 = False
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

    @callback
    def configure_power_fallback(
        self,
        entity_id: str,
        power_on_threshold: float,
        compressor_threshold: float,
    ) -> None:
        """Configure an external power sensor as an honest state fallback."""

        self.power_sensor_entity_id = entity_id
        self._power_on_threshold = power_on_threshold
        self._compressor_threshold = compressor_threshold

    @callback
    def apply_power_fallback(self, watts: float) -> None:
        """Infer power/activity when DP1 is missing or contradicts measured watts."""

        self.power_watts = watts
        self.compressor_active = watts >= self._compressor_threshold
        inferred_power = watts >= self._power_on_threshold
        if (
            DP_POWER in self.state.confirmed_dps
            and self.state.power == inferred_power
        ):
            self.power_fallback_active = False
            self._notify()
            return

        self.state.power = inferred_power
        self.state.source = "power_fallback"
        self.power_fallback_active = True
        self._notify()

    async def async_start(self) -> None:
        """Open one passive Tuya 3.4 session and request one DP update."""

        self._stopping = False
        await self._async_start_monitor_transport(status_requests=False)
        self._poll_task = self.hass.async_create_background_task(
            self._async_poll_loop(), f"emerio_local_poll_{self.device_id}"
        )

    async def _async_start_monitor_transport(
        self, *, status_requests: bool = True
    ) -> bool:
        """Create the persistent monitor without changing the poll task."""

        self._status_requests_enabled = status_requests
        self._passive_updatedps_sent = status_requests
        self._monitor_device = self._new_tuya_device(
            timeout=_STATUS_CONNECTION_TIMEOUT,
            persist=True,
            protocol_version=self._active_protocol,
            force_device22=self._active_device22,
        )
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
            self._monitor_registered = False
            self._status_requests_enabled = False
            self._passive_updatedps_sent = False
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
        self.last_error = (
            None
            if status_requests
            else "Passiver 3.4-Monitor aktiv; warte auf DPS"
        )
        self._notify()

        # The passive transport sends exactly one UPDATEDPS request, then
        # listens only for device pushes and command responses.
        self._schedule_status_request()
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
        self._status_requests_enabled = False
        self._passive_updatedps_sent = False
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
                    # During protocol bootstrap, mirror tuya-local and use a
                    # fresh non-persistent connection. Do not make the socket
                    # persistent until the device has returned real state.
                    async with self._monitor_lock:
                        await self.hass.async_add_executor_job(
                            self._write_dps_sync, dps
                        )
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

    async def async_refresh(self) -> None:
        """Request real DPs and wait briefly for the passive monitor callback."""

        if not self._monitor_registered or not self.monitor_connected:
            if self._monitor_registered:
                await self._async_recover_monitor_status()
            else:
                if not await self._async_start_monitor_transport(
                    status_requests=False
                ):
                    raise EmerioCommunicationError(
                        self.last_error
                        or "Dauerverbindung zum Gerät konnte nicht aufgebaut werden"
                    )
            if not self._monitor_registered:
                raise EmerioCommunicationError(
                    self.last_error
                    or "Dauerverbindung zum Gerät konnte nicht aufgebaut werden"
                )
            return

        waiter = self.hass.loop.create_future()
        self._status_waiters.add(waiter)
        self._schedule_status_request(force=True)
        try:
            await asyncio.wait_for(waiter, timeout=_STATUS_TIMEOUT)
        except TimeoutError:
            if not self._status_requests_enabled:
                self.last_error = "UPDATEDPS: Gerät lieferte keine Datenpunkte"
                self._notify()
                raise EmerioCommunicationError(self.last_error)
            await self._async_recover_monitor_status()
            return
        finally:
            self._status_waiters.discard(waiter)

    async def _async_recover_monitor_status(self) -> None:
        """Replace a stale monitor without issuing a normal status query."""

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
            self._status_requests_enabled = False
            self._passive_updatedps_sent = False
            self.monitor_connected = False
            self.command_reachable = False

            if not await self._async_start_monitor_transport(
                status_requests=False
            ):
                raise EmerioCommunicationError(
                    self.last_error
                    or "Dauerverbindung zum Gerät konnte nicht aufgebaut werden"
                )

    async def _async_bootstrap_then_monitor(self) -> bool:
        """Fetch initial state on fresh sockets before enabling persistence."""

        async with self._monitor_lock:
            if self._monitor_registered:
                return True
            return await self._async_bootstrap_then_monitor_locked()

    async def _async_bootstrap_then_monitor_locked(self) -> bool:
        """Bootstrap status while the monitor lock is held."""

        protocol_label, protocol_version, force_device22 = (
            self._current_bootstrap_protocol
        )
        try:
            dps = await self.hass.async_add_executor_job(
                self._refresh_sync,
                protocol_version,
                force_device22,
            )
        except Exception as err:
            self.monitor_connected = False
            self.command_reachable = False
            self.last_error = f"Status-Bootstrap {protocol_label}: {err}"
            _LOGGER.debug(
                "Initial non-persistent status query to %s with protocol %s failed: %s",
                self.host,
                protocol_label,
                err,
            )
            self._advance_bootstrap_protocol()
            self._notify()
            if self.bootstrap_cycle_exhausted:
                await self._async_start_monitor_transport(status_requests=False)
            return False

        self._active_protocol = protocol_version
        self._active_device22 = force_device22
        self.bootstrap_cycle_exhausted = False
        self.last_status_protocol = protocol_label
        self._apply_device_dps(dps)
        return await self._async_start_monitor_transport()

    @property
    def bootstrap_protocol(self) -> str:
        """Return the protocol variant used by the next bootstrap attempt."""

        return self._current_bootstrap_protocol[0]

    @property
    def status_requests_enabled(self) -> bool:
        """Return whether the persistent transport may actively query status."""

        return self._status_requests_enabled

    @property
    def passive_updatedps_sent(self) -> bool:
        """Return whether the one-shot passive DP refresh was queued."""

        return self._passive_updatedps_sent

    @property
    def _current_bootstrap_protocol(self) -> tuple[str, float, bool]:
        return _BOOTSTRAP_PROTOCOLS[self._bootstrap_protocol_index]

    def _advance_bootstrap_protocol(self) -> None:
        next_index = (self._bootstrap_protocol_index + 1) % len(_BOOTSTRAP_PROTOCOLS)
        self._bootstrap_protocol_index = next_index
        if next_index == 0:
            self.bootstrap_cycle_exhausted = True

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
        if not callable(getattr(handle, "set_multiple_values", None)):
            raise EmerioCommunicationError(
                f"Monitor-Registrierung fehlgeschlagen: {handle!r}"
            )
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
            self._schedule_status_request()
        else:
            self.last_error = f"Verbindungsaufbau: {error}"
        self._notify()

    @callback
    def _handle_monitor_disconnect(self, error: Any) -> None:
        self.monitor_connected = False
        self.command_reachable = False
        self._passive_updatedps_sent = False
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
        if any(int(dp) == DP_POWER for dp in accepted_dps if str(dp).isdigit()):
            self.power_fallback_active = False
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
        if self._monitor is None or self._monitor_device is None:
            raise EmerioCommunicationError("Dauerverbindung ist nicht verfügbar")
        self._monitor.command(self._monitor_device, method, *args)

    @callback
    def _schedule_status_request(self, *, force: bool = False) -> None:
        if not self.monitor_connected or self._stopping:
            return
        if self._status_task is not None and not self._status_task.done():
            if not force:
                return
            self._status_task.cancel()
        if not self._status_requests_enabled:
            if self._passive_updatedps_sent and not force:
                return
            self._passive_updatedps_sent = True
            self._status_task = self.hass.async_create_task(
                self._async_passive_updatedps_request(),
                f"emerio_local_updatedps_{self.device_id}",
            )
            return
        self._status_task = self.hass.async_create_task(
            self._async_status_request(),
            f"emerio_local_status_{self.device_id}",
        )

    async def _async_passive_updatedps_request(self) -> None:
        """Ask once for known DPs without issuing the failing status command."""

        try:
            if not self.monitor_connected:
                return
            self._queue_monitor_command("updatedps", list(KNOWN_DPS))
        except asyncio.CancelledError:
            raise
        except Exception as err:  # pragma: no cover - defensive background boundary
            _LOGGER.debug("Unable to request Emerio DP update: %s", err)

    async def _async_status_request(self) -> None:
        try:
            if not self.monitor_connected:
                return
            self._queue_monitor_command("status")
        except asyncio.CancelledError:
            raise
        except Exception as err:  # pragma: no cover - defensive background boundary
            _LOGGER.debug("Unable to request Emerio status: %s", err)

    async def _async_poll_loop(self) -> None:
        try:
            while True:
                interval = (
                    _POLL_INTERVAL
                    if self._monitor_registered
                    else (
                        _BOOTSTRAP_CYCLE_BACKOFF
                        if self.bootstrap_cycle_exhausted
                        else _BOOTSTRAP_RETRY_INTERVAL
                    )
                )
                await asyncio.sleep(interval)
                await self._async_poll_once()
        except asyncio.CancelledError:
            raise

    async def _async_poll_once(self) -> None:
        """Keep the passive monitor alive without periodic status queries."""

        if self._monitor_registered and self.monitor_connected:
            self._schedule_status_request()
            return

        if self._monitor_registered:
            try:
                await self._async_recover_monitor_status()
            except EmerioCommunicationError:
                return
            return

        await self._async_start_monitor_transport(status_requests=False)

    def _write_dps_sync(self, dps: dict[int, Any]) -> None:
        """Send a command on a fresh socket while status is still bootstrapping."""

        device = self._new_tuya_device(
            timeout=3.0,
            persist=False,
            protocol_version=PROTOCOL_VERSION,
        )
        try:
            payload = {str(dp): value for dp, value in dps.items()}
            result = device.set_multiple_values(payload, nowait=True)
            _raise_for_tuya_error(result)
        finally:
            device.close()

    def _refresh_sync(
        self, protocol_version: float, force_device22: bool
    ) -> dict[str, Any]:
        """Fetch initial DPS on a fresh connection, allowing device22 detection."""

        device = self._new_tuya_device(
            timeout=_STATUS_CONNECTION_TIMEOUT,
            persist=False,
            protocol_version=protocol_version,
            force_device22=force_device22,
        )
        try:
            result = device.status()
            _raise_for_tuya_error(result)
            dps = _extract_dps(result)
            if not dps:
                raise EmerioCommunicationError("Gerät lieferte keine Datenpunkte")
            return dps
        finally:
            device.close()

    def _new_tuya_device(
        self,
        timeout: float,
        persist: bool,
        protocol_version: float,
        force_device22: bool = False,
    ):
        """Create a Tuya transport for bootstrap or persistent monitoring."""

        # TinyTuya's 3.2 setup probes many DPS immediately unless the request
        # list already exists. Construct as 3.3, seed the known DPS, and only
        # then switch to 3.2 to keep every bootstrap attempt to one query.
        constructor_version = 3.3 if protocol_version == 3.2 else protocol_version
        device = tinytuya.OutletDevice(
            self.device_id,
            self.host,
            self._local_key,
            dev_type="default",
            connection_timeout=timeout,
            version=constructor_version,
            persist=persist,
            connection_retry_limit=1,
            connection_retry_delay=0,
        )
        device.set_dpsUsed({str(dp): None for dp in KNOWN_DPS})
        if protocol_version == 3.2:
            device.set_version(3.2)
        if force_device22:
            device.dev_type = "device22"
            device.disabledetect = False
            device.payload_dict = None
        else:
            device.disabledetect = protocol_version < 3.4
        device.set_socketPersistent(persist)
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
