"""
Support for Elation's Obsidian Onyx Lighting Control System.
"""

import asyncio
import logging
import voluptuous as vol
import json
from collections import OrderedDict
from enum import IntEnum
from datetime import timedelta

from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.helpers import (
    config_validation as cv)
from homeassistant.helpers import discovery
from homeassistant.helpers.dispatcher import (
    async_dispatcher_send, async_dispatcher_connect)
from homeassistant.helpers.event import async_track_point_in_utc_time
from homeassistant.util.dt import utcnow


_LOGGER = logging.getLogger(__name__)

READ_SIZE = 1024

DOMAIN = 'onyx'

ONYX_CONTROLLER = 'onyx_controller'

CONFIG_SCHEMA = vol.Schema({
    DOMAIN: vol.Schema({
        vol.Required(CONF_HOST): cv.string,
        vol.Optional(CONF_PORT, default=23): cv.port
    }),
}, extra=vol.ALLOW_EXTRA)

SIGNAL_UPDATE_DATA = 'update_onyx_data'


async def async_setup(hass, config):
    conf = config.get(DOMAIN)

    # Try to connect to Onyx Telnet Server
    host = conf[CONF_HOST]
    port = conf[CONF_PORT]
    interval = timedelta(seconds=2)
    controller = OnyxController(host, port)
    try:
        await asyncio.wait_for(controller.open(), timeout=3)
        await asyncio.wait_for(controller.getCueLists(), timeout=3)
        await asyncio.wait_for(controller.getActiveCueLists(), timeout=3)
    except asyncio.TimeoutError:
        _LOGGER.warning('Onyx Connection Timed Out')
        return False

    hass.data[ONYX_CONTROLLER] = controller
    hass.async_add_job(
        discovery.async_load_platform(
            hass, 'switch', DOMAIN,
            {CONF_HOST: host,
             CONF_PORT: port,
             }, config))

    async def async_update_data(now):
        try:
            await asyncio.wait_for(controller.getActiveCueLists(), timeout=3)
            async_dispatcher_send(hass, SIGNAL_UPDATE_DATA, host)

            async_track_point_in_utc_time(
                hass, async_update_data, utcnow() + interval)
        except asyncio.TimeoutError:
            _LOGGER.warning('Onyx Request Timed Out')

    await async_update_data(None)

    return True


class OnyxController:
    loop = asyncio.get_event_loop()

    class State(IntEnum):
        """State values."""
        Closed = 1
        Opening = 2
        Opened = 3

    def __init__(self, host, port=2323):
        """Initialize OnyxController."""
        self._read_buffer = b""
        self._read_lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()
        self._state = OnyxController.State.Closed
        self._host = host
        self._port = port
        self.active_lists = []
        self.cuelists = []
        self.reader, self.writer = None, None

    def is_connected(self):
        """Returns if the connection is open."""
        return self._state == OnyxController.State.Opened

    async def open(self):
        """Open a Telnet connection to Onyx."""
        with (await self._read_lock):
            with (await self._write_lock):
                if self._state != OnyxController.State.Closed:
                    return
                self._state = OnyxController.State.Opening

                # open connection
                try:
                    fut = asyncio.open_connection(self._host, self._port,
                                                  loop=OnyxController.loop)
                    reader, writer = await asyncio.wait_for(fut, timeout=3)
                except (OSError, asyncio.TimeoutError) as err:
                    _LOGGER.warning(
                        "Error opening connection to Onyx Controller: %s", err)
                    self._state = OnyxController.State.Closed
                    return

                self.reader = reader
                self.writer = writer

                await self.reader.readuntil(b"200 \r\n")

                self._state = OnyxController.State.Opened

                return True

    async def read(self):
        """Return a list of values read from the Telnet interface."""
        with (await self._read_lock):
            try:
                result = await self.reader.readuntil(b".\r\n")
                _LOGGER.debug('< %s', result)
                return result
            except asyncio.streams.IncompleteReadError:
                _LOGGER.warning(
                    'Failed to read from Onyx Controller. Attempting to reconnect...')
                self._state = OnyxController.State.Closed
                await self.open()

    async def write(self, command):
        """Write a list of values out to the Telnet interface."""
        with (await self._write_lock):
            if self._state != OnyxController.State.Opened:
                return
            try:
                _LOGGER.debug('> Sent Command: ' + command)
                self.writer.write((command + "\r\n").encode())
            except OSError as err:
                _LOGGER.warning(
                    "Error writing out to the Onyx Controller: %s", err)

    async def getCueLists(self):
        """Request list of all available cuelists."""
        await self.write('QLList')
        result = await self.read()
        res = result.decode("utf-8").splitlines()
        cuelists = []
        for i in res:
            if (i != '200 Ok' and i != '.'):
                cl = i.split(' - ')
                cuelists.append({cl[0]: cl[1]})

        _LOGGER.debug('getCueLists: ' + str(cuelists))
        self.cuelists = cuelists
        return cuelists

    async def getActiveCueLists(self):
        # Query for active cue lists
        try:
            await asyncio.wait_for(self.write('QLActive'), timeout=3)
        except asyncio.TimeoutError:
            _LOGGER.warning('Get Active CueLists Timed Out')
            return False

        result = await self.read()
        res = result.decode("utf-8").splitlines()
        cuelists = []
        for i in res:
            if (i != '200 Ok' and i != '.' and i != 'No Active Qlist in List'):
                cl = i.split(' - ')
                cuelists.append(cl[0])

        _LOGGER.debug('getActiveCueLists: ' + str(cuelists))
        self.active_lists = cuelists
        return cuelists

    async def goCueList(self, cuelist):
        await self.write('GQL {}'.format(cuelist))
        res = await self.read()
        return True

    async def releaseCueList(self, cuelist):
        await self.write('RQL {}'.format(cuelist))
        res = await self.read()
        return True
