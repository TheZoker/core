"""Support for the Hive alarm."""
from datetime import timedelta

from homeassistant.components.alarm_control_panel import (
    AlarmControlPanelEntity,
    AlarmControlPanelEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    STATE_ALARM_ARMED_AWAY,
    STATE_ALARM_ARMED_NIGHT,
    STATE_ALARM_DISARMED,
    STATE_ALARM_TRIGGERED,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import HiveEntity
from .const import DOMAIN

ICON = "mdi:security"
PARALLEL_UPDATES = 0
SCAN_INTERVAL = timedelta(seconds=15)
HIVETOHA = {
    "home": STATE_ALARM_DISARMED,
    "asleep": STATE_ALARM_ARMED_NIGHT,
    "away": STATE_ALARM_ARMED_AWAY,
}


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up Hive thermostat based on a config entry."""

    hive = hass.data[DOMAIN][entry.entry_id]
    if devices := hive.session.deviceList.get("alarm_control_panel"):
        async_add_entities(
            [HiveAlarmControlPanelEntity(hive, dev) for dev in devices], True
        )


class HiveAlarmControlPanelEntity(HiveEntity, AlarmControlPanelEntity):
    """Representation of a Hive alarm."""

    _attr_icon = ICON
    _attr_supported_features = (
        AlarmControlPanelEntityFeature.ARM_NIGHT
        | AlarmControlPanelEntityFeature.ARM_AWAY
    )

    async def async_alarm_disarm(self, code=None):
        """Send disarm command."""
        await self.hive.alarm.setMode(self.device, "home")

    async def async_alarm_arm_night(self, code=None):
        """Send arm night command."""
        await self.hive.alarm.setMode(self.device, "asleep")

    async def async_alarm_arm_away(self, code=None):
        """Send arm away command."""
        await self.hive.alarm.setMode(self.device, "away")

    async def async_update(self):
        """Update all Node data from Hive."""
        await self.hive.session.updateData(self.device)
        self.device = await self.hive.alarm.getAlarm(self.device)
        self._attr_available = self.device["deviceData"].get("online")
        if self._attr_available:
            if self.device["status"]["state"]:
                self._attr_state = STATE_ALARM_TRIGGERED
            else:
                self._attr_state = HIVETOHA[self.device["status"]["mode"]]
