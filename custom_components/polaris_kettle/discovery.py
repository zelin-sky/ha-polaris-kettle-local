"""Discovery helper for Polaris Kettle."""
from __future__ import annotations

import logging
import threading
from typing import Callable, Dict, List, Optional

import zeroconf

_LOGGER = logging.getLogger(__name__)
_LOGGER.setLevel(logging.DEBUG)

SERVICE_TYPE = "_syncleo._udp.local."

DiscoveryCallback = Callable[[dict], None]


def normalize_mac(mac: str) -> str:
    """Return MAC as 12 lowercase hex characters without separators."""
    return mac.replace(":", "").replace("-", "").lower()


def device_signature(info: Optional[dict]) -> Optional[tuple]:
    """Fingerprint of everything the UDP connection depends on.

    After a power cycle the kettle may come back with another IP address,
    UDP port or public key. If any of them differs from what the current
    connection was created with, the connection must be rebuilt.
    """
    if not info:
        return None
    return (
        tuple(sorted(info.get("addresses", []))),
        info.get("port"),
        info.get("public_key"),
        info.get("curve"),
        info.get("protocol"),
    )


class SyncleoDiscovery:
    """Class to handle Syncleo device discovery."""

    _instance = None

    @classmethod
    def get_instance(cls) -> SyncleoDiscovery:
        """Get singleton discovery instance."""
        if cls._instance is None:
            cls._instance = SyncleoDiscovery()
        return cls._instance

    def __init__(self):
        """Initialize the discovery."""
        self._devices: Dict[str, dict] = {}
        self._listeners: Dict[str, List[DiscoveryCallback]] = {}
        self._zc: Optional[zeroconf.Zeroconf] = None
        self._browser: Optional[zeroconf.ServiceBrowser] = None
        self._lock = threading.Lock()
        self._started = False
        self._is_shared_zeroconf = False

    # ------------------------------------------------------------------
    # Setup
    # ------------------------------------------------------------------

    @property
    def zeroconf_instance(self) -> Optional[zeroconf.Zeroconf]:
        """Zeroconf instance used by the discovery (if any)."""
        return self._zc

    def set_zeroconf_instance(self, zc: zeroconf.Zeroconf) -> None:
        """Set shared Zeroconf instance."""
        self._zc = zc
        self._is_shared_zeroconf = True
        _LOGGER.debug("Set shared Zeroconf instance")

    def start_discovery(self) -> None:
        """Start discovering devices."""
        if self._started:
            _LOGGER.debug("Discovery already started")
            return

        try:
            # Якщо Zeroconf instance не встановлено, створюємо власний
            if self._zc is None:
                _LOGGER.info("Creating internal Zeroconf instance for discovery")
                self._zc = zeroconf.Zeroconf()
                self._is_shared_zeroconf = False
            else:
                _LOGGER.info("Using shared Zeroconf instance for discovery")

            self._browser = zeroconf.ServiceBrowser(self._zc, SERVICE_TYPE, self)
            self._started = True
            _LOGGER.info("Started Syncleo device discovery")
        except Exception as err:
            _LOGGER.error("Failed to start discovery: %s", err)

    def stop_discovery(self) -> None:
        """Stop discovering devices."""
        if not self._started:
            return

        if self._browser:
            self._browser.cancel()
            self._browser = None

        # Закриваємо лише якщо це наш власний екземпляр
        if self._zc and not self._is_shared_zeroconf:
            self._zc.close()
            self._zc = None

        with self._lock:
            self._devices.clear()

        self._started = False
        _LOGGER.info("Stopped Syncleo device discovery")

    # ------------------------------------------------------------------
    # Public API used by the config flow and the coordinator
    # ------------------------------------------------------------------

    def get_devices(self) -> List[dict]:
        """Get list of discovered devices."""
        with self._lock:
            return list(self._devices.values())

    def get_device(self, mac: str) -> Optional[dict]:
        """Return the last valid discovery record of a device (a copy)."""
        with self._lock:
            device = self._devices.get(normalize_mac(mac))
            return dict(device) if device else None

    def add_listener(self, mac: str, callback: DiscoveryCallback) -> Callable[[], None]:
        """Subscribe to add/update announcements of one device.

        The callback is called from a zeroconf thread with a valid discovery
        record. Returns a function that removes the subscription.
        """
        mac = normalize_mac(mac)
        with self._lock:
            self._listeners.setdefault(mac, []).append(callback)

        def _remove() -> None:
            with self._lock:
                callbacks = self._listeners.get(mac)
                if callbacks and callback in callbacks:
                    callbacks.remove(callback)
                    if not callbacks:
                        del self._listeners[mac]

        return _remove

    def request_device(self, mac: str, timeout_ms: int = 2000) -> Optional[dict]:
        """Actively query the network for a device.

        Blocking, must be called from an executor thread. Updates the cache
        but does not notify listeners.
        """
        zc = self._zc
        if zc is None:
            return None

        mac = normalize_mac(mac)
        with self._lock:
            known = self._devices.get(mac)
            name = known["name"] if known else f"{mac}.{SERVICE_TYPE}"

        try:
            info = zc.get_service_info(SERVICE_TYPE, name, timeout=timeout_ms)
        except Exception as err:
            _LOGGER.debug("Active query for %s failed: %s", mac, err)
            return None
        if not info:
            return None

        device = self._parse_service(name, info)
        if device is None:
            return None
        with self._lock:
            self._devices[mac] = device
        return dict(device)

    # ------------------------------------------------------------------
    # zeroconf.ServiceListener
    # ------------------------------------------------------------------

    def add_service(self, zc: zeroconf.Zeroconf, type_: str, name: str) -> None:
        """Service added callback."""
        self._update_service("add_service", zc, type_, name)

    def update_service(self, zc: zeroconf.Zeroconf, type_: str, name: str) -> None:
        """Service updated callback."""
        self._update_service("update_service", zc, type_, name)

    def remove_service(self, zc: zeroconf.Zeroconf, type_: str, name: str) -> None:
        """Service removed callback."""
        _LOGGER.debug("Service removed: %s", name)
        mac = self._extract_mac_from_name(name)
        if mac:
            with self._lock:
                if mac in self._devices:
                    del self._devices[mac]
            _LOGGER.info("Device removed: %s", mac)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _update_service(self, method: str, zc: zeroconf.Zeroconf, type_: str, name: str) -> None:
        """Handle service updates."""
        try:
            info = zc.get_service_info(type_, name)
            if not info:
                return

            device = self._parse_service(name, info)
            if device is None:
                # Incomplete record (e.g. empty TXT while the kettle is still
                # booting): keep the previous valid data instead of
                # overwriting it with 'Unknown'/empty values.
                _LOGGER.debug("%s: ignoring incomplete record for %s", method, name)
                return

            mac = device["mac"]
            with self._lock:
                self._devices[mac] = device
                callbacks = list(self._listeners.get(mac, ()))

            addresses = device["addresses"]
            _LOGGER.info("Discovered device (%s): %s - %s:%s (devtype: %s, vendor: %s)",
                         method, mac, addresses[0] if addresses else 'unknown', device["port"],
                         device['devtype'], device['vendor'])

            for callback in callbacks:
                try:
                    callback(dict(device))
                except Exception:  # noqa: BLE001
                    _LOGGER.exception("Discovery listener for %s failed", mac)

        except Exception as exc:
            _LOGGER.error("Error processing service update: %s", exc)

    def _parse_service(self, name: str, info: zeroconf.ServiceInfo) -> Optional[dict]:
        """Convert ServiceInfo to a device dict. None if the record is unusable."""
        mac = self._extract_mac_from_name(name)
        if not mac:
            return None

        # Convert properties with proper error handling
        properties = {}
        if info.properties:
            for key, value in info.properties.items():
                try:
                    key_str = key.decode('utf-8') if isinstance(key, bytes) else str(key)
                    value_str = value.decode('utf-8') if isinstance(value, bytes) else str(value)
                    properties[key_str] = value_str
                except Exception as prop_err:
                    _LOGGER.debug("Error decoding property %s: %s", key, prop_err)
                    continue

        # Convert addresses to strings (IPv4 only, skip link-local)
        addresses = []
        if info.addresses:
            for addr in info.addresses:
                if len(addr) != 4:
                    continue
                ip = ".".join(str(b) for b in addr)
                if ip.startswith("169.254."):
                    continue
                addresses.append(ip)

        if not properties.get('public') or not addresses or not info.port:
            return None

        return {
            'mac': mac,
            'name': name,
            'addresses': addresses,
            'port': info.port,
            'devtype': properties.get('devtype', 'Unknown'),
            'vendor': properties.get('vendor', 'Unknown'),
            'basetype': properties.get('basetype', 'Unknown'),
            'firmware': properties.get('firmware', 'Unknown'),
            'public_key': properties.get('public', ''),
            'curve': properties.get('curve', ''),
            'protocol': properties.get('protocol', ''),
        }

    def _extract_mac_from_name(self, name: str) -> Optional[str]:
        """Extract MAC address from service name."""
        try:
            if name.startswith('_') or '.' not in name:
                return None

            mac_part = name.split('.')[0].lower()
            # Check if it looks like a MAC address (12 hex characters)
            if len(mac_part) == 12 and all(c in '0123456789abcdef' for c in mac_part):
                return mac_part
            return None
        except Exception:
            return None
