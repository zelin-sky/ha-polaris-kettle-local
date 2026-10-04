"""Data update coordinator for Polaris Kettle."""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import timedelta
from typing import Any, Optional

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import DOMAIN, POLARIS_DEVICE
from .discovery import SyncleoDiscovery, device_signature
from .kettle import Kettle
from .protocol import (
    PowerType,
    IncomingMessageListener,
    ConnectionStatusListener,
    ConnectionStatus,
    CurrentTemperatureMessage,
    ModeMessage,
    TargetTemperatureMessage,
    ChildLockMessage,
    VolumeMessage,
    BacklightMessage,
    NightMessage,
    ColorNightMessage,
    DeviceHardwareMessage,
    ErrorMessage,
    WeightMessage
)

_LOGGER = logging.getLogger(__name__)
_LOGGER.setLevel(logging.DEBUG)

# Keys of the discovery record that are stored in the config entry, so that the
# entities can be created even when the kettle is switched off at HA startup.
STATIC_INFO_KEYS = ("vendor", "basetype", "devtype", "firmware")

WATCHDOG_INTERVAL = 30       # s, period of the connection health check
STUCK_TIMEOUT = 90           # s, a live connection thread that never reaches CONNECTED
DISCOVERY_DEBOUNCE = 1.0     # s, wait after the kettle announced itself (it may still be booting)
RECONNECT_BACKOFF_STEP = 5   # s, retry delay grows by this step per failed attempt ...
RECONNECT_BACKOFF_MAX = 60   # s, ... up to this value
RECENT_CONNECT_WINDOW = 20   # s, an unchanged announcement during a handshake does not restart it


class PolarisDataUpdateCoordinator(DataUpdateCoordinator, IncomingMessageListener, ConnectionStatusListener):
    """Class to manage fetching Polaris data.

    Connection supervision
    ----------------------
    The kettle encrypts the UDP session with its public key and announces the
    address, port and key over mDNS. After the kettle lost power it comes back
    with a fresh mDNS record, so a connection built from the old record can
    never be restored. Therefore the coordinator

    * subscribes to the shared SyncleoDiscovery and rebuilds the connection
      whenever the announced address/port/key differ from the current ones
      (or the kettle is not connected);
    * revives a connection thread that gave up (DISCONNECTED) with a growing
      delay, always re-reading the freshest discovery data first;
    * runs a periodic watchdog as a safety net.

    All of it goes through a single reconnect task, so there is never more
    than one reconnection in flight.
    """

    def __init__(self, hass: HomeAssistant, mac: str, device_token: str,
                 static_info: Optional[dict] = None) -> None:
        """Initialize global kettle data updater."""
        self.kettle = Kettle(mac, device_token)
        self._hass = hass
        self._mac = mac
        self._device_token = device_token
        self._static_info = static_info

        super().__init__(
            hass,
            _LOGGER,
            name=f"Polaris Kettle {mac}",
            update_interval=timedelta(seconds=WATCHDOG_INTERVAL),
        )

        self.data = {
            "current_temperature": None,
            "target_temperature": None,
            "power_type": PowerType.OFF,
            "is_heating": False,
            "child_lock": False,
            "volume": False,
            "backlight": False,
            "night": False,
            "color_night": {"r": 0, "g": 0, "b": 0},
            "error": False,
            "connected": False,
            "device_hardware": None,
            "weight": None,
        }

        # Device info будет установлен после discovery
        self.device_info = None
        self._discovery = SyncleoDiscovery.get_instance()
        self._unsub_discovery = None
        self._discovered_device_info: Optional[dict] = None  # record the connection was built from
        self._last_signature = None
        self._pending_info: Optional[dict] = None             # fresh record pushed by mDNS
        self._setup_complete = False
        self._shutting_down = False

        self._reconnect_task: Optional[asyncio.Task] = None
        self._reconnect_again = False
        self._wake = asyncio.Event()
        self._reconnect_attempts = 0
        self._connect_started = 0.0
        self._not_connected_since: Optional[float] = time.monotonic()

    @property
    def discovered_info(self) -> Optional[dict]:
        """Last discovery record used to connect to the kettle."""
        return self._discovered_device_info

    # ------------------------------------------------------------------
    # Setup / shutdown
    # ------------------------------------------------------------------

    async def async_setup(self, zeroconf_instance: Any = None, discovered_device_info: dict = None) -> None:
        """Set up the kettle connection."""
        # Если уже настроено, не делаем ничего
        if self._setup_complete:
            _LOGGER.debug("Kettle already setup, skipping")
            return

        self._shutting_down = False
        try:
            if zeroconf_instance is not None and self._discovery.zeroconf_instance is None:
                self._discovery.set_zeroconf_instance(zeroconf_instance)
            # idempotent
            await self._hass.async_add_executor_job(self._discovery.start_discovery)

            # Subscribe first so that no announcement is missed while we look around
            self._unsub_discovery = self._discovery.add_listener(self._mac, self._discovery_callback)

            info = discovered_device_info or self._discovery.get_device(self._mac)
            if info is None:
                info = await self._hass.async_add_executor_job(
                    self._discovery.request_device, self._mac)

            if info is not None:
                _LOGGER.info("Using discovered device info")
                self._create_device_info_from_dict(info)
                await self._async_connect(info)
            elif self._static_info:
                # The kettle is off (or not reachable) right now. Entities are created
                # from the stored info; the connection starts as soon as it shows up.
                _LOGGER.warning("Device %s not found on the network, will connect when it appears",
                                self._mac)
                self._create_device_info_from_dict(self._static_info)
            else:
                raise UpdateFailed("device not found on the network and its model is not known yet")

            self._setup_complete = True
            _LOGGER.info("Kettle setup completed successfully")

        except Exception as err:
            _LOGGER.error("Failed to setup kettle: %s", err)
            await self.shutdown()
            raise UpdateFailed(f"Setup failed: {err}") from err

    async def shutdown(self) -> None:
        """Shutdown the kettle connection."""
        self._shutting_down = True

        if self._unsub_discovery is not None:
            self._unsub_discovery()
            self._unsub_discovery = None

        task = self._reconnect_task
        self._reconnect_task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

        def stop():
            self.kettle.stop_all()

        await self._hass.async_add_executor_job(stop)

    def _create_device_info_from_dict(self, device_info: dict) -> None:
        """Create device info from a discovery / stored record."""
        vendor = device_info.get('vendor', 'Polaris')
        basetype = device_info.get('basetype', '00')
        devtype = device_info.get('devtype', '00')
        firmware = device_info.get('firmware', '0.00')

        # Получаем модель из POLARIS_DEVICE или используем базовый тип
        try:
            model = POLARIS_DEVICE[int(devtype)]['model']
        except (KeyError, ValueError):
            model = f"Type {devtype}"
            _LOGGER.warning("Unknown device type: %s, using default model", devtype)

        _LOGGER.info(f"Device info: vendor={vendor}, basetype={basetype}, devtype={devtype}, firmware={firmware}, model={model}")

        self.device_info = DeviceInfo(
            identifiers={(DOMAIN, self._mac)},
            name=f"{vendor} {model} {self._mac}",
            manufacturer=vendor,
            model=model,
            sw_version=firmware,
            model_id=devtype,
        )

    # ------------------------------------------------------------------
    # Connection supervisor
    # ------------------------------------------------------------------

    async def _async_connect(self, info: dict) -> None:
        """(Re)create the UDP connection from a discovery record."""
        self._discovered_device_info = info
        self._last_signature = device_signature(info)
        self._connect_started = time.monotonic()
        await self._hass.async_add_executor_job(self._connect_sync, info)

    def _connect_sync(self, info: dict) -> None:
        """Executor part of _async_connect."""
        if self._shutting_down:
            return
        self.kettle.update_device_info(info)
        self.kettle.restart_connection(self, self)
        if self._shutting_down:
            # shutdown() ran while we were starting the thread
            self.kettle.stop_all()

    def _discovery_callback(self, info: dict) -> None:
        """Called from a zeroconf thread when the kettle announces itself."""
        try:
            self._hass.loop.call_soon_threadsafe(self._async_handle_discovery, info)
        except RuntimeError:
            # event loop is closed (HA is stopping)
            pass

    @callback
    def _async_handle_discovery(self, info: dict) -> None:
        """The kettle announced (or re-announced) itself on the network."""
        if self._shutting_down or not self._setup_complete:
            return

        changed = device_signature(info) != self._last_signature
        if not changed:
            if self.kettle.is_connected():
                # periodic mDNS refresh, nothing to do
                return
            conn = self.kettle.conn
            if (conn is not None and conn.is_alive()
                    and time.monotonic() - self._connect_started < RECENT_CONNECT_WINDOW):
                # a handshake with exactly these parameters is in progress
                return

        _LOGGER.info("Kettle %s announced itself (%s), reconnecting",
                     self._mac, "new address/port/key" if changed else "was not connected")
        self._pending_info = info
        self._async_schedule_reconnect(delay=DISCOVERY_DEBOUNCE, urgent=True)

    @callback
    def _async_schedule_reconnect(self, delay: float = 0.0, urgent: bool = False) -> None:
        """Make sure a reconnect task is running.

        urgent=True (fresh data from the kettle) wakes a sleeping task up and
        makes it run once more after the current attempt.
        """
        if self._shutting_down:
            return

        task = self._reconnect_task
        if task is not None and not task.done():
            if urgent:
                self._reconnect_again = True
                self._wake.set()
            return

        self._reconnect_task = self._hass.async_create_task(self._async_reconnect(delay))

    async def _sleep_or_wake(self, delay: float) -> None:
        self._wake.clear()
        if delay <= 0:
            return
        try:
            await asyncio.wait_for(self._wake.wait(), timeout=delay)
        except asyncio.TimeoutError:
            pass

    async def _async_reconnect(self, delay: float) -> None:
        """Rebuild the connection using the freshest device info available."""
        try:
            while True:
                self._reconnect_again = False
                await self._sleep_or_wake(delay)
                if self._shutting_down:
                    return

                info = self._pending_info
                self._pending_info = None
                if info is None:
                    # ask the network first: the cache may still hold the record of
                    # the previous power-on session
                    info = await self._hass.async_add_executor_job(
                        self._discovery.request_device, self._mac)
                if info is None:
                    info = self._discovery.get_device(self._mac)
                if info is None:
                    # Not visible right now. The kettle may still be back with the
                    # same address/key (short outage), so try the last known record.
                    info = self._discovered_device_info

                if info is None:
                    _LOGGER.debug("Kettle %s: no discovery data yet, waiting", self._mac)
                else:
                    level = logging.INFO if self._reconnect_attempts == 0 else logging.DEBUG
                    _LOGGER.log(level, "Kettle %s: reconnecting to %s:%s (attempt %s)",
                                self._mac, (info.get("addresses") or ["?"])[0], info.get("port"),
                                self._reconnect_attempts + 1)
                    self._reconnect_attempts += 1
                    await self._async_connect(info)

                if not self._reconnect_again:
                    return
                delay = DISCOVERY_DEBOUNCE
        except asyncio.CancelledError:
            raise
        except Exception as err:  # noqa: BLE001
            # the watchdog / the next status event will try again
            _LOGGER.error("Kettle %s: reconnect failed: %s", self._mac, err)
        finally:
            if self._reconnect_task is asyncio.current_task():
                self._reconnect_task = None

    async def _async_update_data(self) -> dict[str, Any]:
        """Watchdog: runs periodically and revives a dead connection."""
        conn = self.kettle.conn
        alive = conn is not None and conn.is_alive()
        connected = alive and self.kettle.is_connected()
        was_connected = self.data.get("connected")

        if connected:
            self._not_connected_since = None
            self.data["connected"] = True
        else:
            now = time.monotonic()
            if self._not_connected_since is None:
                self._not_connected_since = now
            self.data["connected"] = False

            stuck = alive and now - self._not_connected_since > STUCK_TIMEOUT
            if self._setup_complete and (not alive or stuck):
                _LOGGER.debug("Watchdog: %s",
                              "connection thread is not running" if not alive else "connection is stuck")
                self._async_schedule_reconnect()

        if was_connected != self.data["connected"]:
            # data is the same dict object, the base class would not notice the change
            self.async_update_listeners()
        return self.data

    # ------------------------------------------------------------------
    # Messages and status from the connection thread
    # ------------------------------------------------------------------

    def incoming_message(self, message) -> None:
        """Handle incoming messages from kettle."""
        _LOGGER.debug("Received message: %s", message)

        # Schedule update in event loop
        self._hass.loop.call_soon_threadsafe(
            self._async_handle_incoming_message, message
        )

    @callback
    def _async_handle_incoming_message(self, message) -> None:
        """Handle incoming messages in event loop."""
        if isinstance(message, CurrentTemperatureMessage):
            self.data["current_temperature"] = message.current_temperature
            # Determine if heating based on temperature and power state
            current_temp = self.data["current_temperature"]
            target_temp = self.data["target_temperature"]
            power_type = self.data["power_type"]

            # Нагрев происходит когда устройство включено и текущая температура меньше целевой
            if (power_type != PowerType.OFF and
                current_temp is not None and
                target_temp is not None and
                current_temp < target_temp):
                self.data["is_heating"] = True
            else:
                self.data["is_heating"] = False

        elif isinstance(message, ModeMessage):
            self.data["power_type"] = message.pt
            if message.pt == PowerType.OFF:
                self.data["is_heating"] = False

        elif isinstance(message, TargetTemperatureMessage):
            self.data["target_temperature"] = message.temperature

        elif isinstance(message, ChildLockMessage):
            self.data["child_lock"] = message.value

        elif isinstance(message, VolumeMessage):
            self.data["volume"] = message.value

        elif isinstance(message, BacklightMessage):
            self.data["backlight"] = message.value

        elif isinstance(message, NightMessage):
            self.data["night"] = message.value

        elif isinstance(message, ColorNightMessage):
            self.data["color_night"] = {
                "r": message.r,
                "g": message.g,
                "b": message.b,
                "w": message.w,
                "data_length": message.data_length
            }
        elif isinstance(message, WeightMessage):
            self.data["weight"] = message.weight
            _LOGGER.debug("---WEIGHT--- %s grams", message.weight)

        elif isinstance(message, DeviceHardwareMessage):
            self.data["device_hardware"] = message.hw
            _LOGGER.debug("---HARDWARE--- %s", message.hw)

        elif isinstance(message, ErrorMessage):
            self.data["error"] = message.value

        # Schedule update for entities
        self.async_set_updated_data(self.data)

    def connection_status_updated(self, status: ConnectionStatus) -> None:
        """Handle connection status updates."""
        _LOGGER.debug("Connection status updated: %s", status)

        # Schedule update in event loop
        self._hass.loop.call_soon_threadsafe(
            self._async_handle_connection_status, status
        )

    @callback
    def _async_handle_connection_status(self, status: ConnectionStatus) -> None:
        """Handle connection status updates in event loop."""
        if self._shutting_down:
            return

        self.data["connected"] = status == ConnectionStatus.CONNECTED

        if status == ConnectionStatus.CONNECTED:
            if self._not_connected_since is not None or self._reconnect_attempts:
                _LOGGER.info("Kettle %s is connected", self._mac)
            self._not_connected_since = None
            self._reconnect_attempts = 0
        else:
            if self._not_connected_since is None:
                self._not_connected_since = time.monotonic()

            # RECONNECTING is handled by the connection thread itself (3 handshake
            # attempts). When it gives up it reports DISCONNECTED and its thread
            # ends - from here on only we can bring the kettle back.
            if status == ConnectionStatus.DISCONNECTED:
                delay = min(RECONNECT_BACKOFF_STEP * (self._reconnect_attempts + 1),
                            RECONNECT_BACKOFF_MAX)
                _LOGGER.debug("Connection gave up, reconnecting in %s s", delay)
                self._async_schedule_reconnect(delay=delay)

        self.async_set_updated_data(self.data)

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------

    async def async_set_power(self, power_type: PowerType) -> None:
        """Set kettle power state."""
        def set_power():
            self.kettle.set_power(power_type, lambda x: _LOGGER.debug("Power set callback: %s", x))

        await self._hass.async_add_executor_job(set_power)

    async def async_set_temperature(self, temperature: int) -> None:
        """Set target temperature."""
        def set_temperature():
            self.kettle.set_target_temperature(temperature, lambda x: _LOGGER.debug("Temperature set callback: %s", x))

        await self._hass.async_add_executor_job(set_temperature)

    async def async_set_child_lock(self, enabled: bool) -> None:
        """Set child lock state."""
        def set_child_lock():
            self.kettle.set_child_lock(enabled, lambda x: _LOGGER.debug(f"Child lock set callback: {x}"))

        await self._hass.async_add_executor_job(set_child_lock)

    async def async_set_volume(self, enabled: bool) -> None:
        """Set volume state."""
        def set_volume():
            self.kettle.set_volume(enabled, lambda x: _LOGGER.debug(f"Volume set callback: {x}"))

        await self._hass.async_add_executor_job(set_volume)

    async def async_set_backlight(self, enabled: bool) -> None:
        """Set backlight state."""
        def set_backlight():
            self.kettle.set_backlight(enabled, lambda x: _LOGGER.debug(f"Backlight set callback: {x}"))

        await self._hass.async_add_executor_job(set_backlight)

    async def async_set_night(self, enabled: bool) -> None:
        """Set night state."""
        def set_night():
            self.kettle.set_night(enabled, lambda x: _LOGGER.debug(f"Night set callback: {x}"))

        await self._hass.async_add_executor_job(set_night)

    async def async_set_color_night(self, r: int, g: int, b: int, w: int = 0, data_length: int = 4) -> None:
        """Set color night state with variable data length."""
        def set_color_night():
            self.kettle.set_color_night(r, g, b, w, data_length,
                                       lambda x: _LOGGER.debug(f"Color night set callback: {x}"))

        await self._hass.async_add_executor_job(set_color_night)
