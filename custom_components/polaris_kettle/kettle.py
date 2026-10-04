from __future__ import annotations

import threading
import logging
import socket
import zeroconf
import time
from abc import abstractmethod
from ipaddress import ip_address, IPv4Address, IPv6Address
from typing import Optional, List, Union

from .protocol import (
    UDPConnection,
    ModeMessage,
    ChildLockMessage,
    VolumeMessage,
    BacklightMessage,
    NightMessage,
    ColorNightMessage,
    TargetTemperatureMessage,
    PowerType,
    ConnectionStatus,
    ConnectionStatusListener,
    WrappedMessage
)

_LOGGER = logging.getLogger(__name__)
_LOGGER.setLevel(logging.DEBUG)

class DeviceDiscover(threading.Thread, zeroconf.ServiceListener):
    si: Optional[zeroconf.ServiceInfo]
    _mac: str
    _sb: Optional[zeroconf.ServiceBrowser]
    _zc: Optional[zeroconf.Zeroconf]
    _listeners: List[DeviceListener]
    _valid_addresses: List[Union[IPv4Address, IPv6Address]]
    _only_ipv4: bool
    _use_shared_zeroconf: bool

    def __init__(self, mac: str,
                 listener: Optional[DeviceListener] = None,
                 only_ipv4=True,
                 zeroconf_instance: Optional[zeroconf.Zeroconf] = None):
        super().__init__()
        self.si = None
        self._mac = mac
        self._zc = zeroconf_instance  # Use shared instance
        self._sb = None
        self._only_ipv4 = only_ipv4
        self._valid_addresses = []
        self._listeners = []
        if isinstance(listener, DeviceListener):
            self._listeners.append(listener)
        self._logger = logging.getLogger(f'{__name__}.{self.__class__.__name__}')
        self._use_shared_zeroconf = zeroconf_instance is not None

    def add_listener(self, listener: DeviceListener):
        if listener not in self._listeners:
            self._listeners.append(listener)
        else:
            self._logger.warning(f'add_listener: listener {listener} already in the listeners list')

    def set_info(self, info: zeroconf.ServiceInfo, notify: bool = True):
        # Перевіряємо, що properties не порожні
        if not info.properties:
            self._logger.debug("set_info: ignoring service info with empty properties")
            return
            
        valid_addresses = self._get_valid_addresses(info)
        if not valid_addresses:
            raise ValueError('no valid addresses')
        self._valid_addresses = valid_addresses
        self.si = info
        
        # Додаємо налагоджувальне логування
        self._logger.debug(f"set_info: received service info with properties: {info.properties}")
        if info.properties:
            for key, value in info.properties.items():
                key_str = key.decode('utf-8') if isinstance(key, bytes) else str(key)
                value_str = value.decode('utf-8') if isinstance(value, bytes) else str(value)
                self._logger.debug(f"set_info: property {key_str} = {value_str}")
        
        if not notify:
            return

        for f in self._listeners:
            try:
                f.device_updated()
            except Exception as exc:
                self._logger.error(f'set_info: error while calling device_updated on {f}')
                self._logger.exception(exc)

    def add_service(self, zc: zeroconf.Zeroconf, type_: str, name: str) -> None:
        self._add_update_service('add_service', zc, type_, name)

    def update_service(self, zc: zeroconf.Zeroconf, type_: str, name: str) -> None:
        self._add_update_service('update_service', zc, type_, name)

    def _add_update_service(self, method: str, zc: zeroconf.Zeroconf, type_: str, name: str) -> None:
        info = zc.get_service_info(type_, name)
        if name.startswith(f'{self._mac}.'):
            self._logger.info(f'{method}: type={type_} name={name}')
            
            # Ігноруємо сповіщення з порожніми властивостями
            if not info.properties:
                self._logger.debug(f'{method}: ignoring due to empty properties')
                return
                
            try:
                self.set_info(info)
            except ValueError as exc:
                self._logger.error(f'{method}: rejected: {str(exc)}')
        else:
            self._logger.debug(f'{method}: mac not matched: {info}')

    def remove_service(self, zc: zeroconf.Zeroconf, type_: str, name: str) -> None:
        if name.startswith(f'{self._mac}.'):
            self._logger.info(f'remove_service: type={type_} name={name}')
            # TODO what to do here?!

    def run(self):
        self._logger.debug('starting zeroconf service browser')
        
        # Only create Zeroconf instance if not provided
        if self._zc is None:
            ip_version = zeroconf.IPVersion.V4Only if self._only_ipv4 else zeroconf.IPVersion.All
            self._zc = zeroconf.Zeroconf(ip_version=ip_version)
        
        self._sb = zeroconf.ServiceBrowser(self._zc, "_syncleo._udp.local.", self)
        self._sb.join()

    def stop(self):
        if self._sb:
            try:
                self._sb.cancel()
            except RuntimeError:
                pass
            self._sb = None
        
        # Only close Zeroconf if we created it (not shared instance)
        if self._zc is not None and not self._use_shared_zeroconf:
            self._zc.close()
        self._zc = None

    def _get_valid_addresses(self, si: zeroconf.ServiceInfo) -> List[Union[IPv4Address, IPv6Address]]:
        """Get valid addresses from service info."""
        valid = []
        if not si.addresses:
            return valid
            
        for addr_bytes in si.addresses:
            try:
                addr = ip_address(addr_bytes)
                if self._only_ipv4 and not isinstance(addr, IPv4Address):
                    continue
                if isinstance(addr, IPv4Address) and str(addr).startswith('169.254.'):
                    continue
                valid.append(addr)
            except Exception as exc:
                self._logger.debug("Error processing address %s: %s", addr_bytes, exc)
        return valid

    @property
    def pubkey(self) -> bytes:
        """Get public key from service properties."""
        if not self.si or not self.si.properties:
            raise ValueError("No properties available")
        
        # Пробуємо отримати як bytes, якщо не виходить - як рядок
        pubkey_hex = self.si.properties.get(b'public')
        if pubkey_hex is None:
            # Пробуємо отримати як рядок (якщо властивості вже декодовано)
            pubkey_hex_str = None
            for key, value in self.si.properties.items():
                key_str = key.decode('utf-8') if isinstance(key, bytes) else str(key)
                if key_str == 'public':
                    pubkey_hex_str = value.decode('utf-8') if isinstance(value, bytes) else str(value)
                    break
            
            if not pubkey_hex_str:
                raise ValueError("Public key not found in properties")
            
            try:
                return bytes.fromhex(pubkey_hex_str)
            except Exception as exc:
                raise ValueError(f"Invalid public key format: {exc}")
        else:
            try:
                return bytes.fromhex(pubkey_hex.decode('utf-8'))
            except Exception as exc:
                raise ValueError(f"Invalid public key format: {exc}")

    @property
    def curve(self) -> int:
        """Get curve type from service properties."""
        if not self.si or not self.si.properties:
            raise ValueError("No properties available")
        
        # Пробуємо отримати як bytes, якщо не виходить - як рядок
        curve_str = self.si.properties.get(b'curve')
        if curve_str is None:
            # Пробуємо отримати як рядок (якщо властивості вже декодовано)
            curve_str_val = None
            for key, value in self.si.properties.items():
                key_str = key.decode('utf-8') if isinstance(key, bytes) else str(key)
                if key_str == 'curve':
                    curve_str_val = value.decode('utf-8') if isinstance(value, bytes) else str(value)
                    break
            
            if not curve_str_val:
                raise ValueError("Curve not found in properties")
            
            try:
                return int(curve_str_val)
            except Exception as exc:
                raise ValueError(f"Invalid curve format: {exc}")
        else:
            try:
                return int(curve_str.decode('utf-8'))
            except Exception as exc:
                raise ValueError(f"Invalid curve format: {exc}")

    @property
    def addr(self) -> Union[IPv4Address, IPv6Address]:
        return self._valid_addresses[0]

    @property
    def port(self) -> int:
        return int(self.si.port)

    @property
    def protocol(self) -> int:
        """Get protocol version from service properties."""
        if not self.si or not self.si.properties:
            raise ValueError("No properties available")
        
        # Пробуємо отримати як bytes, якщо не виходить - як рядок
        protocol_str = self.si.properties.get(b'protocol')
        if protocol_str is None:
            # Пробуємо отримати як рядок (якщо властивості вже декодовано)
            protocol_str_val = None
            for key, value in self.si.properties.items():
                key_str = key.decode('utf-8') if isinstance(key, bytes) else str(key)
                if key_str == 'protocol':
                    protocol_str_val = value.decode('utf-8') if isinstance(value, bytes) else str(value)
                    break
            
            if not protocol_str_val:
                raise ValueError("Protocol not found in properties")
            
            try:
                return int(protocol_str_val)
            except Exception as exc:
                raise ValueError(f"Invalid protocol format: {exc}")
        else:
            try:
                return int(protocol_str.decode('utf-8'))
            except Exception as exc:
                raise ValueError(f"Invalid protocol format: {exc}")


class DeviceListener:
    @abstractmethod
    def device_updated(self):
        pass


class _ScopedStatusListener(ConnectionStatusListener):
    """Forward status updates only while ``conn`` is still the current connection.

    A replaced connection thread can still emit a last RECONNECTING /
    DISCONNECTED event. Without this filter such a stale event would be taken
    for the state of the new connection.
    """

    def __init__(self, kettle: "Kettle", conn: UDPConnection, target: ConnectionStatusListener):
        self._kettle = kettle
        self._conn = conn
        self._target = target

    def connection_status_updated(self, status: ConnectionStatus):
        if self._kettle.conn is not self._conn:
            return
        self._target.connection_status_updated(status)


class Kettle(DeviceListener, ConnectionStatusListener):
    mac: str
    device: Optional[DeviceDiscover]
    device_token: str
    conn: Optional[UDPConnection]
    conn_status: Optional[ConnectionStatus]
    _read_timeout: Optional[int]
    _logger: logging.Logger
    _find_evt: threading.Event

    def __init__(self, mac: str, device_token: str, read_timeout: Optional[int] = None):
        super().__init__()
        self.mac = mac
        self.device = None
        self.device_token = device_token
        self.conn = None
        self.conn_status = None
        self._read_timeout = read_timeout
        self._find_evt = threading.Event()
        self._logger = logging.getLogger(f'{__name__}.{self.__class__.__name__}[{mac}]')  # Додаємо MAC до логера

    # ------------------------------------------------------------------
    # Device info (address / port / public key) and connection management
    # ------------------------------------------------------------------

    @staticmethod
    def _build_service_info(info: dict) -> zeroconf.ServiceInfo:
        """Build a ServiceInfo from a discovery record."""
        addresses = []
        for ip in info['addresses']:
            try:
                addresses.append(socket.inet_pton(socket.AF_INET, ip))
            except OSError:
                # Пропускаємо невалідні адреси
                continue

        # properties as bytes, as zeroconf expects
        properties = {
            b'public': str(info['public_key']).encode('utf-8'),
            b'curve': str(info['curve']).encode('utf-8'),
            b'protocol': str(info['protocol']).encode('utf-8'),
            b'vendor': str(info.get('vendor', '')).encode('utf-8'),
            b'basetype': str(info.get('basetype', '')).encode('utf-8'),
            b'devtype': str(info.get('devtype', '')).encode('utf-8'),
            b'firmware': str(info.get('firmware', '')).encode('utf-8'),
        }

        return zeroconf.ServiceInfo(
            type_="_syncleo._udp.local.",
            name=info['name'],
            addresses=addresses,
            port=info['port'],
            properties=properties,
            server=info['name'].split('.')[0] + '.local.',
        )

    def update_device_info(self, info: dict) -> None:
        """Replace the cached address/port/public key with fresh discovery data.

        Raises ValueError if the record has no usable address.
        """
        service_info = self._build_service_info(info)
        if self.device is None:
            self.device = DeviceDiscover(self.mac, listener=self, only_ipv4=True)
        # The zeroconf browser of DeviceDiscover is never started: updates are
        # delivered by SyncleoDiscovery, so no listener notification here.
        self.device.set_info(service_info, notify=False)
        self._find_evt.set()

    def device_updated(self):
        """DeviceListener callback (not used: SyncleoDiscovery feeds the info)."""
        self._find_evt.set()

    def connection_status_updated(self, status: ConnectionStatus):
        self.conn_status = status

    def force_reconnect(self):
        """Stop the current connection thread (blocking, call from an executor)."""
        conn = self.conn
        # detach first so that late status events of the old thread are ignored
        self.conn = None
        self.conn_status = None
        if conn is not None:
            self._logger.info("Stopping the current connection")
            conn.stop_connection()
        else:
            self._logger.debug("No active connection to stop")

    def restart_connection(self,
                           incoming_message_listener=None,
                           connection_status_listener=None) -> bool:
        """Drop the current connection and start a new one from the cached device info.

        Blocking, call from an executor. Returns True if a thread was started.
        """
        self.force_reconnect()
        self.start_server_if_needed(incoming_message_listener, connection_status_listener)
        return self.conn is not None

    def start_server_if_needed(self,
                               incoming_message_listener=None,
                               connection_status_listener=None) -> bool:
        # Перевіряємо, що device ініціалізовано
        if not self.device:
            self._logger.error("Device not initialized, cannot start server")
            return False

        if not self.device.si:
            self._logger.error("Device service info not available, cannot start server")
            return False

        # Якщо з'єднання існує, але потік не живий - створюємо заново
        if self.conn and not self.conn.is_alive():
            self._logger.info("Connection thread is dead, recreating...")
            self.conn = None

        if self.conn:
            self._logger.warning('start_server_if_needed: server is already started!')
            # Оновлюємо параметри наявного з'єднання
            self.conn.set_address(self.device.addr, self.device.port)
            self.conn.set_device_pubkey(self.device.pubkey)
            return True

        # Перевіряємо, що device має всі необхідні властивості
        try:
            curve = self.device.curve
            protocol = self.device.protocol
            pubkey = self.device.pubkey
            self._logger.debug(f"Device properties - curve: {curve}, protocol: {protocol}, pubkey: {pubkey.hex()[:16]}...")
        except Exception as exc:
            self._logger.error(f"Missing device properties: {exc}")
            return False

        if curve != 29:
            raise ValueError(f'curve type {curve} is not implemented')
        if protocol != 2:
            raise ValueError(f'protocol {protocol} is not supported')

        kw = {}
        if self._read_timeout is not None:
            kw['read_timeout'] = self._read_timeout

        # Створюємо нове з'єднання
        conn = UDPConnection(addr=self.device.addr,
                             port=self.device.port,
                             device_pubkey=pubkey,
                             device_token=bytes.fromhex(self.device_token), **kw)
        self.conn = conn
        self.conn_status = ConnectionStatus.NOT_CONNECTED

        if incoming_message_listener:
            conn.add_incoming_message_listener(incoming_message_listener)

        conn.add_connection_status_listener(_ScopedStatusListener(self, conn, self))
        if connection_status_listener:
            conn.add_connection_status_listener(
                _ScopedStatusListener(self, conn, connection_status_listener))

        conn.start()
        self._logger.info(f"New UDP connection started ({self.device.addr}:{self.device.port})")
        return True

    def stop_all(self):
        # when we stop server, we should also stop device discovering service
        if self.conn:
            self.conn.interrupted = True
            self.conn = None
        if self.device:
            self.device.stop()
            self.device = None

    def is_connected(self) -> bool:
        return self.conn is not None and self.conn_status == ConnectionStatus.CONNECTED

    def set_power(self, power_type: PowerType, callback: callable):
        if self.conn is None:
            self._logger.error("Cannot set power: not connected")
            callback(False)
            return
            
        message = ModeMessage(power_type)
        self.conn.enqueue_message(WrappedMessage(message, handler=callback, ack=True))

    def set_target_temperature(self, temp: int, callback: callable):
        if self.conn is None:
            self._logger.error("Cannot set temperature: not connected")
            callback(False)
            return
            
        message = TargetTemperatureMessage(temp)
        self.conn.enqueue_message(WrappedMessage(message, handler=callback, ack=True))
        

    def set_child_lock(self, enabled: bool, callback: callable):
        """Set child lock state."""
        _LOGGER.debug("ChildLockMessage: %s", enabled)
        if self.conn is None:
            self._logger.error("Cannot set child lock: not connected")
            callback(False)
            return
            
        message = ChildLockMessage(enabled)
        self.conn.enqueue_message(WrappedMessage(message, handler=callback, ack=True))

    def set_volume(self, enabled: bool, callback: callable):
        """Set volume state."""
        if self.conn is None:
            self._logger.error("Cannot set volume: not connected")
            callback(False)
            return
            
        message = VolumeMessage(enabled)
        self.conn.enqueue_message(WrappedMessage(message, handler=callback, ack=True))

    def set_backlight(self, enabled: bool, callback: callable):
        """Set backlight state."""
        if self.conn is None:
            self._logger.error("Cannot set backlight: not connected")
            callback(False)
            return
            
        message = BacklightMessage(enabled)
        self.conn.enqueue_message(WrappedMessage(message, handler=callback, ack=True))

    def set_night(self, enabled: bool, callback: callable):
        """Set night state."""
        if self.conn is None:
            self._logger.error("Cannot set night: not connected")
            callback(False)
            return
            
        message = NightMessage(enabled)
        self.conn.enqueue_message(WrappedMessage(message, handler=callback, ack=True))

    def set_color_night(self, r: int, g: int, b: int, w: int = 0, data_length: int = 4, callback: callable = None):
        """Set color night state with variable data length."""
        if self.conn is None:
            self._logger.error("Cannot set color night: not connected")
            if callback:
                callback(False)
            return
            
        message = ColorNightMessage(r, g, b, w, data_length)
        self.conn.enqueue_message(WrappedMessage(message, handler=callback, ack=True))