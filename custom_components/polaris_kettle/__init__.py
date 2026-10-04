"""The Polaris Kettle integration."""
from __future__ import annotations

import logging
import asyncio
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady

from .coordinator import PolarisDataUpdateCoordinator, STATIC_INFO_KEYS
from .discovery import SyncleoDiscovery

_LOGGER = logging.getLogger(__name__)
_LOGGER.setLevel(logging.DEBUG)

PLATFORMS: list[Platform] = [Platform.WATER_HEATER, Platform.SWITCH, Platform.LIGHT, Platform.SENSOR]

async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Set up the Polaris Kettle component."""
    # Попередньо налаштовуємо discovery зі спільним Zeroconf
    try:
        from homeassistant.components.zeroconf import async_get_instance
        zc = await async_get_instance(hass)
        discovery = SyncleoDiscovery.get_instance()
        discovery.set_zeroconf_instance(zc)
        
        # Запускаємо discovery заздалегідь
        await hass.async_add_executor_job(discovery.start_discovery)
        _LOGGER.info("Pre-started Syncleo device discovery with shared Zeroconf")
    except Exception as err:
        _LOGGER.warning("Could not pre-start discovery: %s", err)
    
    return True

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Polaris Kettle from a config entry."""

    mac = entry.data["mac"].replace(":", "").lower()
    device_token = entry.data["device_token"]

    # Model info saved when the entry was created. It lets the entities be
    # created even if the kettle is switched off while HA starts.
    static_info = {k: entry.data[k] for k in STATIC_INFO_KEYS if entry.data.get(k)} or None

    coordinator = PolarisDataUpdateCoordinator(hass, mac, device_token, static_info)

    # Get shared Zeroconf instance
    try:
        from homeassistant.components.zeroconf import async_get_instance
        zc = await async_get_instance(hass)
    except ImportError:
        _LOGGER.warning("Zeroconf not available, falling back to internal instance")
        zc = None

    # Setup the kettle connection. The coordinator looks the device up itself
    # (discovery cache, then an active mDNS query) and keeps the connection alive
    # afterwards, including after the kettle lost power.
    try:
        await coordinator.async_setup(zc)
        await coordinator.async_config_entry_first_refresh()
    except Exception as err:
        _LOGGER.error("Device %s setup failed: %s", mac, err)
        await coordinator.shutdown()
        raise ConfigEntryNotReady(f"Device setup failed: {err}") from err

    # Remember the model for the next start (entries created by older versions lack it)
    info = coordinator.discovered_info
    if info and not all(entry.data.get(k) for k in ("devtype", "vendor")):
        extra = {k: info[k] for k in STATIC_INFO_KEYS if info.get(k) and info[k] != "Unknown"}
        if extra:
            hass.config_entries.async_update_entry(entry, data={**entry.data, **extra})

    hass.data.setdefault(entry.domain, {})
    hass.data[entry.domain][entry.entry_id] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True

async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        coordinator: PolarisDataUpdateCoordinator = hass.data[entry.domain].pop(entry.entry_id)
        await coordinator.shutdown()
    
    return unload_ok