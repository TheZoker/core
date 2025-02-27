"""Support for hunter douglas shades."""
from __future__ import annotations

import asyncio
from collections.abc import Iterable
from contextlib import suppress
from datetime import timedelta
import logging
from typing import Any

from aiopvapi.helpers.constants import (
    ATTR_POSITION1,
    ATTR_POSITION2,
    ATTR_POSITION_DATA,
)
from aiopvapi.resources.shade import (
    ATTR_POSKIND1,
    ATTR_POSKIND2,
    MAX_POSITION,
    MIN_POSITION,
    BaseShade,
    ShadeTdbu,
    Silhouette,
    factory as PvShade,
)
import async_timeout

from homeassistant.components.cover import (
    ATTR_POSITION,
    ATTR_TILT_POSITION,
    CoverDeviceClass,
    CoverEntity,
    CoverEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_call_later

from .const import (
    COORDINATOR,
    DEVICE_INFO,
    DEVICE_MODEL,
    DOMAIN,
    LEGACY_DEVICE_MODEL,
    POS_KIND_PRIMARY,
    POS_KIND_SECONDARY,
    POS_KIND_VANE,
    PV_API,
    PV_ROOM_DATA,
    PV_SHADE_DATA,
    ROOM_ID_IN_SHADE,
    ROOM_NAME_UNICODE,
    STATE_ATTRIBUTE_ROOM_NAME,
)
from .coordinator import PowerviewShadeUpdateCoordinator
from .entity import ShadeEntity
from .shade_data import PowerviewShadeMove

_LOGGER = logging.getLogger(__name__)

# Estimated time it takes to complete a transition
# from one state to another
TRANSITION_COMPLETE_DURATION = 40

PARALLEL_UPDATES = 1

RESYNC_DELAY = 60

# this equates to 0.75/100 in terms of hass blind position
# some blinds in a closed position report less than 655.35 (1%)
# but larger than 0 even though they are clearly closed
# Find 1 percent of MAX_POSITION, then find 75% of that number
# The means currently 491.5125 or less is closed position
# implemented for top/down shades, but also works fine with normal shades
CLOSED_POSITION = (0.75 / 100) * (MAX_POSITION - MIN_POSITION)

SCAN_INTERVAL = timedelta(minutes=10)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up the hunter douglas shades."""

    pv_data = hass.data[DOMAIN][entry.entry_id]
    room_data: dict[str | int, Any] = pv_data[PV_ROOM_DATA]
    shade_data = pv_data[PV_SHADE_DATA]
    pv_request = pv_data[PV_API]
    coordinator: PowerviewShadeUpdateCoordinator = pv_data[COORDINATOR]
    device_info: dict[str, Any] = pv_data[DEVICE_INFO]

    entities: list[ShadeEntity] = []
    for raw_shade in shade_data.values():
        # The shade may be out of sync with the hub
        # so we force a refresh when we add it if possible
        shade: BaseShade = PvShade(raw_shade, pv_request)
        name_before_refresh = shade.name
        with suppress(asyncio.TimeoutError):
            async with async_timeout.timeout(1):
                await shade.refresh()

        if ATTR_POSITION_DATA not in shade.raw_data:
            _LOGGER.info(
                "The %s shade was skipped because it is missing position data",
                name_before_refresh,
            )
            continue
        coordinator.data.update_shade_positions(shade.raw_data)
        room_id = shade.raw_data.get(ROOM_ID_IN_SHADE)
        room_name = room_data.get(room_id, {}).get(ROOM_NAME_UNICODE, "")
        entities.extend(
            create_powerview_shade_entity(
                coordinator, device_info, room_name, shade, name_before_refresh
            )
        )
    async_add_entities(entities)


def create_powerview_shade_entity(
    coordinator: PowerviewShadeUpdateCoordinator,
    device_info: dict[str, Any],
    room_name: str,
    shade: BaseShade,
    name_before_refresh: str,
) -> Iterable[ShadeEntity]:
    """Create a PowerViewShade entity."""
    classes: list[BaseShade] = []
    # order here is important as both ShadeTDBU are listed in aiovapi as can_tilt
    # and both require their own class here to work
    if isinstance(shade, ShadeTdbu):
        classes.extend([PowerViewShadeTDBUTop, PowerViewShadeTDBUBottom])
    elif isinstance(shade, Silhouette):
        classes.append(PowerViewShadeSilhouette)
    elif shade.can_tilt:
        classes.append(PowerViewShadeWithTilt)
    else:
        classes.append(PowerViewShade)
    return [
        cls(coordinator, device_info, room_name, shade, name_before_refresh)
        for cls in classes
    ]


def hd_position_to_hass(hd_position: int, max_val: int = MAX_POSITION) -> int:
    """Convert hunter douglas position to hass position."""
    return round((hd_position / max_val) * 100)


def hass_position_to_hd(hass_position: int, max_val: int = MAX_POSITION) -> int:
    """Convert hass position to hunter douglas position."""
    return int(hass_position / 100 * max_val)


class PowerViewShadeBase(ShadeEntity, CoverEntity):
    """Representation of a powerview shade."""

    _attr_device_class = CoverDeviceClass.SHADE
    _attr_supported_features = 0

    def __init__(
        self,
        coordinator: PowerviewShadeUpdateCoordinator,
        device_info: dict[str, Any],
        room_name: str,
        shade: BaseShade,
        name: str,
    ) -> None:
        """Initialize the shade."""
        super().__init__(coordinator, device_info, room_name, shade, name)
        self._shade: BaseShade = shade
        self._attr_name = self._shade_name
        self._scheduled_transition_update: CALLBACK_TYPE | None = None
        if self._device_info[DEVICE_MODEL] != LEGACY_DEVICE_MODEL:
            self._attr_supported_features |= CoverEntityFeature.STOP
        self._forced_resync = None

    @property
    def assumed_state(self) -> bool:
        """If the device is hard wired we are polling state.

        The hub will frequently provide the wrong state
        for battery power devices so we set assumed
        state in this case.
        """
        return not self._is_hard_wired

    @property
    def should_poll(self) -> bool:
        """Only poll if the device is hard wired.

        We cannot poll battery powered devices
        as it would drain their batteries in a matter
        of days.
        """
        return self._is_hard_wired

    @property
    def extra_state_attributes(self) -> dict[str, str]:
        """Return the state attributes."""
        return {STATE_ATTRIBUTE_ROOM_NAME: self._room_name}

    @property
    def is_closed(self):
        """Return if the cover is closed."""
        return self.positions.primary <= CLOSED_POSITION

    @property
    def current_cover_position(self) -> int:
        """Return the current position of cover."""
        return hd_position_to_hass(self.positions.primary, MAX_POSITION)

    @property
    def transition_steps(self) -> int:
        """Return the steps to make a move."""
        return hd_position_to_hass(self.positions.primary, MAX_POSITION)

    @property
    def open_position(self) -> PowerviewShadeMove:
        """Return the open position and required additional positions."""
        return PowerviewShadeMove(self._shade.open_position, {})

    @property
    def close_position(self) -> PowerviewShadeMove:
        """Return the close position and required additional positions."""
        return PowerviewShadeMove(self._shade.close_position, {})

    async def async_close_cover(self, **kwargs: Any) -> None:
        """Close the cover."""
        self._async_schedule_update_for_transition(self.transition_steps)
        await self._async_execute_move(self.close_position)
        self._attr_is_opening = False
        self._attr_is_closing = True
        self.async_write_ha_state()

    async def async_open_cover(self, **kwargs: Any) -> None:
        """Open the cover."""
        self._async_schedule_update_for_transition(100 - self.transition_steps)
        await self._async_execute_move(self.open_position)
        self._attr_is_opening = True
        self._attr_is_closing = False
        self.async_write_ha_state()

    async def async_stop_cover(self, **kwargs: Any) -> None:
        """Stop the cover."""
        self._async_cancel_scheduled_transition_update()
        self.data.update_from_response(await self._shade.stop())
        await self._async_force_refresh_state()

    @callback
    def _clamp_cover_limit(self, target_hass_position: int) -> int:
        """Dont allow a cover to go into an impossbile position."""
        # no override required in base
        return target_hass_position

    async def async_set_cover_position(self, **kwargs: Any) -> None:
        """Move the shade to a specific position."""
        await self._async_set_cover_position(kwargs[ATTR_POSITION])

    @callback
    def _get_shade_move(self, target_hass_position: int) -> PowerviewShadeMove:
        position_one = hass_position_to_hd(target_hass_position)
        return PowerviewShadeMove(
            {ATTR_POSITION1: position_one, ATTR_POSKIND1: POS_KIND_PRIMARY}, {}
        )

    async def _async_execute_move(self, move: PowerviewShadeMove) -> None:
        """Execute a move that can affect multiple positions."""
        response = await self._shade.move(move.request)
        # Process any positions we know will update as result
        # of the request since the hub won't return them
        for kind, position in move.new_positions.items():
            self.data.update_shade_position(self._shade.id, position, kind)
        # Finally process the response
        self.data.update_from_response(response)

    async def _async_set_cover_position(self, target_hass_position: int) -> None:
        """Move the shade to a position."""
        target_hass_position = self._clamp_cover_limit(target_hass_position)
        current_hass_position = self.current_cover_position
        self._async_schedule_update_for_transition(
            abs(current_hass_position - target_hass_position)
        )
        await self._async_execute_move(self._get_shade_move(target_hass_position))
        self._attr_is_opening = target_hass_position > current_hass_position
        self._attr_is_closing = target_hass_position < current_hass_position
        self.async_write_ha_state()

    @callback
    def _async_update_shade_data(self, shade_data: dict[str | int, Any]) -> None:
        """Update the current cover position from the data."""
        self.data.update_shade_positions(shade_data)
        self._attr_is_opening = False
        self._attr_is_closing = False

    @callback
    def _async_cancel_scheduled_transition_update(self) -> None:
        """Cancel any previous updates."""
        if self._scheduled_transition_update:
            self._scheduled_transition_update()
            self._scheduled_transition_update = None
        if self._forced_resync:
            self._forced_resync()
            self._forced_resync = None

    @callback
    def _async_schedule_update_for_transition(self, steps: int) -> None:
        # Cancel any previous updates
        self._async_cancel_scheduled_transition_update()

        est_time_to_complete_transition = 1 + int(
            TRANSITION_COMPLETE_DURATION * (steps / 100)
        )

        _LOGGER.debug(
            "Estimated time to complete transition of %s steps for %s: %s",
            steps,
            self.name,
            est_time_to_complete_transition,
        )

        # Schedule an forced update for when we expect the transition
        # to be completed.
        self._scheduled_transition_update = async_call_later(
            self.hass,
            est_time_to_complete_transition,
            self._async_complete_schedule_update,
        )

    async def _async_complete_schedule_update(self, _):
        """Update status of the cover."""
        _LOGGER.debug("Processing scheduled update for %s", self.name)
        self._scheduled_transition_update = None
        await self._async_force_refresh_state()
        self._forced_resync = async_call_later(
            self.hass, RESYNC_DELAY, self._async_force_resync
        )

    async def _async_force_resync(self, *_: Any) -> None:
        """Force a resync after an update since the hub may have stale state."""
        self._forced_resync = None
        _LOGGER.debug("Force resync of shade %s", self.name)
        await self._async_force_refresh_state()

    async def _async_force_refresh_state(self) -> None:
        """Refresh the cover state and force the device cache to be bypassed."""
        await self.async_update()
        self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        """When entity is added to hass."""
        self.async_on_remove(
            self.coordinator.async_add_listener(self._async_update_shade_from_group)
        )

    async def async_will_remove_from_hass(self) -> None:
        """Cancel any pending refreshes."""
        self._async_cancel_scheduled_transition_update()

    @property
    def _update_in_progress(self) -> bool:
        """Check if an update is already in progress."""
        return bool(self._scheduled_transition_update or self._forced_resync)

    @callback
    def _async_update_shade_from_group(self) -> None:
        """Update with new data from the coordinator."""
        if self._update_in_progress:
            # If a transition is in progress the data will be wrong
            return
        self.data.update_from_group_data(self._shade.id)
        self.async_write_ha_state()

    async def async_update(self) -> None:
        """Refresh shade position."""
        if self._update_in_progress:
            # The update will likely timeout and
            # error if are already have one in flight
            return
        await self._shade.refresh()
        self._async_update_shade_data(self._shade.raw_data)


class PowerViewShade(PowerViewShadeBase):
    """Represent a standard shade."""

    def __init__(
        self,
        coordinator: PowerviewShadeUpdateCoordinator,
        device_info: dict[str, Any],
        room_name: str,
        shade: BaseShade,
        name: str,
    ) -> None:
        """Initialize the shade."""
        super().__init__(coordinator, device_info, room_name, shade, name)
        self._attr_supported_features |= (
            CoverEntityFeature.OPEN
            | CoverEntityFeature.CLOSE
            | CoverEntityFeature.SET_POSITION
        )


class PowerViewShadeTDBU(PowerViewShade):
    """Representation of a PowerView shade with top/down bottom/up capabilities."""

    @property
    def transition_steps(self) -> int:
        """Return the steps to make a move."""
        return hd_position_to_hass(
            self.positions.primary, MAX_POSITION
        ) + hd_position_to_hass(self.positions.secondary, MAX_POSITION)


class PowerViewShadeTDBUBottom(PowerViewShadeTDBU):
    """Representation of a top down bottom up powerview shade."""

    def __init__(
        self,
        coordinator: PowerviewShadeUpdateCoordinator,
        device_info: dict[str, Any],
        room_name: str,
        shade: BaseShade,
        name: str,
    ) -> None:
        """Initialize the shade."""
        super().__init__(coordinator, device_info, room_name, shade, name)
        self._attr_unique_id = f"{self._shade.id}_bottom"
        self._attr_name = f"{self._shade_name} Bottom"

    @callback
    def _clamp_cover_limit(self, target_hass_position: int) -> int:
        """Dont allow a cover to go into an impossbile position."""
        cover_top = hd_position_to_hass(self.positions.secondary, MAX_POSITION)
        return min(target_hass_position, (100 - cover_top))

    @callback
    def _get_shade_move(self, target_hass_position: int) -> PowerviewShadeMove:
        position_bottom = hass_position_to_hd(target_hass_position)
        position_top = self.positions.secondary
        return PowerviewShadeMove(
            {
                ATTR_POSITION1: position_bottom,
                ATTR_POSITION2: position_top,
                ATTR_POSKIND1: POS_KIND_PRIMARY,
                ATTR_POSKIND2: POS_KIND_SECONDARY,
            },
            {},
        )


class PowerViewShadeTDBUTop(PowerViewShadeTDBU):
    """Representation of a top down bottom up powerview shade."""

    def __init__(
        self,
        coordinator: PowerviewShadeUpdateCoordinator,
        device_info: dict[str, Any],
        room_name: str,
        shade: BaseShade,
        name: str,
    ) -> None:
        """Initialize the shade."""
        super().__init__(coordinator, device_info, room_name, shade, name)
        self._attr_unique_id = f"{self._shade.id}_top"
        self._attr_name = f"{self._shade_name} Top"
        # these shades share a class in parent API
        # override open position for top shade
        self._shade.open_position = {
            ATTR_POSITION1: MIN_POSITION,
            ATTR_POSITION2: MAX_POSITION,
            ATTR_POSKIND1: POS_KIND_PRIMARY,
            ATTR_POSKIND2: POS_KIND_SECONDARY,
        }

    @property
    def is_closed(self):
        """Return if the cover is closed."""
        # top shade needs to check other motor
        return self.positions.secondary <= CLOSED_POSITION

    @property
    def current_cover_position(self) -> int:
        """Return the current position of cover."""
        # these need to be inverted to report state correctly in HA
        return hd_position_to_hass(self.positions.secondary, MAX_POSITION)

    @callback
    def _clamp_cover_limit(self, target_hass_position: int) -> int:
        """Dont allow a cover to go into an impossbile position."""
        cover_bottom = hd_position_to_hass(self.positions.primary, MAX_POSITION)
        return min(target_hass_position, (100 - cover_bottom))

    @callback
    def _get_shade_move(self, target_hass_position: int) -> PowerviewShadeMove:
        position_bottom = self.positions.primary
        position_top = hass_position_to_hd(target_hass_position, MAX_POSITION)
        return PowerviewShadeMove(
            {
                ATTR_POSITION1: position_bottom,
                ATTR_POSITION2: position_top,
                ATTR_POSKIND1: POS_KIND_PRIMARY,
                ATTR_POSKIND2: POS_KIND_SECONDARY,
            },
            {},
        )


class PowerViewShadeWithTilt(PowerViewShade):
    """Representation of a PowerView shade with tilt capabilities."""

    _max_tilt = MAX_POSITION

    def __init__(
        self,
        coordinator: PowerviewShadeUpdateCoordinator,
        device_info: dict[str, Any],
        room_name: str,
        shade: BaseShade,
        name: str,
    ) -> None:
        """Initialize the shade."""
        super().__init__(coordinator, device_info, room_name, shade, name)
        self._attr_supported_features |= (
            CoverEntityFeature.OPEN_TILT
            | CoverEntityFeature.CLOSE_TILT
            | CoverEntityFeature.SET_TILT_POSITION
        )
        if self._device_info[DEVICE_MODEL] != LEGACY_DEVICE_MODEL:
            self._attr_supported_features |= CoverEntityFeature.STOP_TILT

    @property
    def current_cover_tilt_position(self) -> int:
        """Return the current cover tile position."""
        return hd_position_to_hass(self.positions.vane, self._max_tilt)

    @property
    def transition_steps(self):
        """Return the steps to make a move."""
        return hd_position_to_hass(
            self.positions.primary, MAX_POSITION
        ) + hd_position_to_hass(self.positions.vane, self._max_tilt)

    @property
    def open_position(self) -> PowerviewShadeMove:
        """Return the open position and required additional positions."""
        return PowerviewShadeMove(
            self._shade.open_position, {POS_KIND_VANE: MIN_POSITION}
        )

    @property
    def close_position(self) -> PowerviewShadeMove:
        """Return the close position and required additional positions."""
        return PowerviewShadeMove(
            self._shade.close_position, {POS_KIND_VANE: MIN_POSITION}
        )

    @property
    def open_tilt_position(self) -> PowerviewShadeMove:
        """Return the open tilt position and required additional positions."""
        # next upstream api release to include self._shade.open_tilt_position
        return PowerviewShadeMove(
            {ATTR_POSKIND1: POS_KIND_VANE, ATTR_POSITION1: self._max_tilt},
            {POS_KIND_PRIMARY: MIN_POSITION},
        )

    @property
    def close_tilt_position(self) -> PowerviewShadeMove:
        """Return the close tilt position and required additional positions."""
        # next upstream api release to include self._shade.close_tilt_position
        return PowerviewShadeMove(
            {ATTR_POSKIND1: POS_KIND_VANE, ATTR_POSITION1: MIN_POSITION},
            {POS_KIND_PRIMARY: MIN_POSITION},
        )

    async def async_close_cover_tilt(self, **kwargs: Any) -> None:
        """Close the cover tilt."""
        self._async_schedule_update_for_transition(self.transition_steps)
        await self._async_execute_move(self.close_tilt_position)
        self.async_write_ha_state()

    async def async_open_cover_tilt(self, **kwargs: Any) -> None:
        """Open the cover tilt."""
        self._async_schedule_update_for_transition(100 - self.transition_steps)
        await self._async_execute_move(self.open_tilt_position)
        self.async_write_ha_state()

    async def async_set_cover_tilt_position(self, **kwargs: Any) -> None:
        """Move the vane to a specific position."""
        await self._async_set_cover_tilt_position(kwargs[ATTR_TILT_POSITION])

    async def _async_set_cover_tilt_position(
        self, target_hass_tilt_position: int
    ) -> None:
        """Move the vane to a specific position."""
        final_position = self.current_cover_position + target_hass_tilt_position
        self._async_schedule_update_for_transition(
            abs(self.transition_steps - final_position)
        )
        await self._async_execute_move(self._get_shade_tilt(target_hass_tilt_position))
        self.async_write_ha_state()

    @callback
    def _get_shade_move(self, target_hass_position: int) -> PowerviewShadeMove:
        """Return a PowerviewShadeMove."""
        position_shade = hass_position_to_hd(target_hass_position)
        return PowerviewShadeMove(
            {ATTR_POSITION1: position_shade, ATTR_POSKIND1: POS_KIND_PRIMARY},
            {POS_KIND_VANE: MIN_POSITION},
        )

    @callback
    def _get_shade_tilt(self, target_hass_tilt_position: int) -> PowerviewShadeMove:
        """Return a PowerviewShadeMove."""
        position_vane = hass_position_to_hd(target_hass_tilt_position, self._max_tilt)
        return PowerviewShadeMove(
            {ATTR_POSITION1: position_vane, ATTR_POSKIND1: POS_KIND_VANE},
            {POS_KIND_PRIMARY: MIN_POSITION},
        )

    async def async_stop_cover_tilt(self, **kwargs: Any) -> None:
        """Stop the cover tilting."""
        await self.async_stop_cover()


class PowerViewShadeSilhouette(PowerViewShadeWithTilt):
    """Representation of a Silhouette PowerView shade."""

    _max_tilt = 32767
