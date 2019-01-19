import asyncio
import json
import logging
from datetime import timedelta

from homeassistant.core import callback
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.components.switch import SwitchDevice
from homeassistant.util import Throttle
from homeassistant.helpers.dispatcher import (
    async_dispatcher_send, async_dispatcher_connect)
from ..onyx import OnyxController, SIGNAL_UPDATE_DATA

_LOGGER = logging.getLogger(__name__)

DEPENDENCIES = ['onyx']
MIN_TIME_BETWEEN_UPDATES = timedelta(minutes=5)


async def async_setup_platform(hass, config, async_add_devices, discovery_info=None):
    """Initialize the platform."""
    if discovery_info is None:
        return

    controller = discovery_info['controller']

    devices = [OnyxCueList(cl, controller)
               for cl in controller.cuelists]

    async_add_devices(devices, True)


class OnyxCueList(SwitchDevice):

    def __init__(self, cuelist, controller):
        """Initialize an Onyx switch."""
        for id, name in cuelist.items():
            self._id = id
            self._name = "{}: {}".format(id, name)
        self._controller = controller
        self.entity_id = 'switch.onyx_cl_' + str(self._id)
        self._is_on = False

    async def async_added_to_hass(self):
        """Register update dispatcher."""
        @callback
        def async_onyx_update(host):
            """Update callback."""
            self.async_schedule_update_ha_state(True)

        async_dispatcher_connect(
            self.hass, SIGNAL_UPDATE_DATA, async_onyx_update)

    @property
    def name(self):
        """Return the display name of this switch."""
        return self._name

    @property
    def is_on(self):
        """Return true if switch is on."""
        return self._is_on

    @property
    def available(self):
        """Return True if entity is available."""
        return self._controller._state == OnyxController.State.Opened

    async def async_turn_on(self, **kwargs):
        """Instruct the switch to turn on."""
        _LOGGER.debug("Turn On")
        await self._controller.goCueList(self._id)

    async def async_turn_off(self, **kwargs):
        """Instruct the switch to turn off."""
        _LOGGER.debug("Turn Off")
        await self._controller.releaseCueList(self._id)

    async def async_update(self):
        """Update on/off state."""
        self._is_on = self._id in self._controller.active_lists
