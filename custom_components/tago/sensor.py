"""Power, device location, driver channel, and signal-strength sensors."""
from __future__ import annotations

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import SIGNAL_STRENGTH_DECIBELS_MILLIWATT, UnitOfPower
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo, EntityCategory
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import DOMAIN
from .entity import TagoEntityHA
from .TagoNet import TagoEntity, TagoGateway, TagoDevice
from . import generate_device_info

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    gateway: TagoGateway = config_entry.runtime_data
    entry_id = getattr(config_entry, "entry_id", None)
    if entry_id is not None:
        registry = er.async_get(hass)
        for registered in er.async_entries_for_config_entry(registry, entry_id):
            if registered.domain == "sensor" and registered.platform == DOMAIN and registered.unique_id.endswith(":driver_channel"):
                registry.async_remove(registered.entity_id)
    items: list[SensorEntity] = []
    for device in gateway.devices:
        items.append(DeviceLocationSensor(device))
        items.append(DeviceSerialNumberSensor(device))
    for e in gateway.entities:
        # PROTOCOL_PROPOSALS §P6: only entities with a reported `rsi`
        # get a signal-strength companion sensor. An entity that
        # reports rsi=None has no wireless link to measure.
        if e.is_unused():
            continue
        items.append(ProgrammedNameSensor(e))
        if e.is_load:
            items.append(ConsumptionSensor(e))
            if e.is_device_multichannel:
                items.append(ChannelTagSensor(e))
                items.append(ChannelLocationSensor(e))
        if e.rsi is not None:
            items.append(SignalStrengthSensor(e))
    async_add_entities(items)


class DeviceLocationSensor(SensorEntity):
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_has_entity_name = True
    _attr_name = "Location"

    def __init__(self, device: TagoDevice):
        self._device = device
        self._attr_unique_id = f"{device.unique_id}:location"

    @property
    def native_value(self) -> str:
        return self._device.location or "Not specified"

    @property
    def device_info(self) -> DeviceInfo:
        return generate_device_info(self._device)

    @property
    def available(self) -> bool:
        return self._device.is_connected

    @property
    def extra_state_attributes(self) -> dict:
        return {"location": self._device.location}

    async def async_added_to_hass(self) -> None:
        self._device.set_on_state_changed(self.async_write_ha_state)

    async def async_will_remove_from_hass(self) -> None:
        self._device.remove_on_state_changed(self.async_write_ha_state)


class DeviceSerialNumberSensor(SensorEntity):
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_has_entity_name = True
    _attr_name = "Serial Number"

    def __init__(self, device: TagoDevice):
        self._device = device
        self._attr_unique_id = f"{device.unique_id}:serial_number"

    @property
    def native_value(self) -> str:
        return self._device.serial_num or "Not specified"

    @property
    def device_info(self) -> DeviceInfo:
        return generate_device_info(self._device)

    @property
    def available(self) -> bool:
        return self._device.is_connected


class _ChannelSensor(TagoEntityHA, SensorEntity):
    @property
    def device_info(self) -> DeviceInfo:
        identifier = self._entity.unique_id if self._entity.is_device_multichannel else self._entity.device.unique_id
        return DeviceInfo(identifiers={(DOMAIN, identifier)})


class ProgrammedNameSensor(_ChannelSensor):
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def unique_id(self) -> str:
        return f"{self._entity.unique_id}:tago_name"

    @property
    def name(self) -> str:
        return "Name"

    @property
    def native_value(self) -> str:
        return self._entity.name or "Not specified"


class ChannelTagSensor(_ChannelSensor):
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def unique_id(self) -> str:
        return f"{self._entity.unique_id}:tag"

    @property
    def name(self) -> str:
        return "Channel"

    @property
    def native_value(self) -> str:
        return self._entity.tag or "Not specified"


class ChannelLocationSensor(_ChannelSensor):
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def unique_id(self) -> str:
        return f"{self._entity.unique_id}:location"

    @property
    def name(self) -> str:
        return "Location"

    @property
    def native_value(self) -> str:
        return self._entity.location or "Not specified"


class ConsumptionSensor(_ChannelSensor):
    _attr_device_class = SensorDeviceClass.POWER
    _attr_native_unit_of_measurement = UnitOfPower.WATT
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 3

    @property
    def unique_id(self) -> str:
        return f"{self._entity.unique_id}:power"

    @property
    def name(self) -> str:
        return "Power Consumption"

    @property
    def native_value(self) -> float | None:
        return self._entity.power_watts


class SignalStrengthSensor(_ChannelSensor):
    """Per-entity RSSI sensor (PROTOCOL_PROPOSALS §P6).

    Attaches to the parent entity's device-registry entry so it shows
    up under the same device card. Diagnostic + disabled-by-default
    — most users won't care about RSSI day-to-day, but the data is
    there when diagnosing dropouts."""

    _attr_device_class = SensorDeviceClass.SIGNAL_STRENGTH
    _attr_native_unit_of_measurement = SIGNAL_STRENGTH_DECIBELS_MILLIWATT
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False

    @property
    def unique_id(self) -> str:
        return f"{self._entity.unique_id}:rsi"

    @property
    def name(self) -> str:
        # `has_entity_name=True` (inherited from TagoEntityHA) makes HA
        # compose `friendly_name = <device_card_name> + " " + name`. The
        # device card already carries the parent entity's name, so we
        # return only the suffix here — returning `"<parent> Signal
        # Strength"` would double the parent label on the entity row.
        return "Signal Strength"

    @property
    def native_value(self) -> int | None:
        return self._entity.rsi

    @property
    def device_info(self) -> DeviceInfo | None:
        # For keypads the integration registers the device card up
        # front (see __init__._async_register_keypads_and_dispatch_events);
        # this sensor's `identifiers` already match that card, so HA
        # merges them onto a single entry rather than creating a sibling.
        return super().device_info
