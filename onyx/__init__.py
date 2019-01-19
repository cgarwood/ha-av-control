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

    hass.async_add_job(
        discovery.async_load_platform(
            hass, 'switch', DOMAIN,
            {CONF_HOST: host,
             CONF_PORT: port,
             'controller': controller
             }, config))

    async def async_update_data(now):
        try:
            await asyncio.wait_for(controller.getActiveCueLists(), timeout=3)
            async_dispatcher_send(hass, SIGNAL_UPDATE_DATA, host)

            async_track_point_in_utc_time(
                hass, async_update_data, utcnow() + interval)
        except asyncio.TimeoutError:
            _LOGGER.debug('Onyx Request Timed Out')

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

    @asyncio.coroutine
    def open(self):
        """Open a Telnet connection to Onyx."""
        with (yield from self._read_lock):
            with (yield from self._write_lock):
                if self._state != OnyxController.State.Closed:
                    return
                self._state = OnyxController.State.Opening

                # open connection
                try:
                    fut = asyncio.open_connection(self._host, self._port,
                                                  loop=OnyxController.loop)
                    reader, writer = yield from asyncio.wait_for(fut, timeout=3)
                except OSError as err:
                    _LOGGER.warning(
                        "Error opening connection to Onyx Controller: %s", err)
                    self._state = OnyxController.State.Closed
                    return

                self.reader = reader
                self.writer = writer

                yield from self._read_until(b"200 \r\n")

                self._state = OnyxController.State.Opened

                return True

    @asyncio.coroutine
    def _read_until(self, value):
        """Read until a given value is reached."""
        while True:
            if (self._read_buffer != b''):
                _LOGGER.debug(self._read_buffer)

            if hasattr(value, "search"):
                # assume regular expression
                match = value.search(self._read_buffer)
                if match:
                    self._read_buffer = self._read_buffer[match.end():]
                    return match
            else:
                where = self._read_buffer.find(value)
                if where != -1:
                    # self._read_buffer = self._read_buffer[where + len(value):]
                    res = self._read_buffer
                    self._read_buffer = b""
                    return res
            try:
                self._read_buffer += yield from self.reader.read(READ_SIZE)
            except OSError as err:
                _LOGGER.warning(
                    "Error reading from Onyx Controller: %s", err)
                return False

    @asyncio.coroutine
    def read(self):
        """Return a list of values read from the Telnet interface."""
        with (yield from self._read_lock):
            if self._state != OnyxController.State.Opened:
                return None
            match = yield from self._read_until(b".\r\n")
            if match is not False:
                try:
                    return match
                except ValueError:
                    print("Exception in ", match)
        if match is False:
            # attempt to reconnect
            _LOGGER.info("Reconnecting to Onyx Controller %s", self._host)
            self._state = OnyxController.State.Closed
            yield from self.open()
        return None

    @asyncio.coroutine
    def write(self, command):
        """Write a list of values out to the Telnet interface."""
        with (yield from self._write_lock):
            if self._state != OnyxController.State.Opened:
                return
            try:
                _LOGGER.debug('> ' + command)
                self.writer.write((command + "\r\n").encode())
            except OSError as err:
                _LOGGER.warning(
                    "Error writing out to the Onyx Controller: %s", err)

    @asyncio.coroutine
    def getCueLists(self):
        """Request list of all available cuelists."""
        yield from self.write('QLList')
        result = yield from self.read()
        res = result.decode("utf-8").splitlines()
        cuelists = []
        for i in res:
            if (i != '200 Ok' and i != '.'):
                cl = i.split(' - ')
                cuelists.append({cl[0]: cl[1]})

        _LOGGER.debug('getCueLists: ' + str(cuelists))
        self.cuelists = cuelists
        return cuelists

    @asyncio.coroutine
    def getActiveCueLists(self):
        # Query for active cue lists
        yield from self.write('QLActive')
        result = yield from self.read()
        res = result.decode("utf-8").splitlines()
        cuelists = []
        for i in res:
            if (i != '200 Ok' and i != '.'):
                cl = i.split(' - ')
                cuelists.append(cl[0])

        _LOGGER.debug('getActiveCueLists: ' + str(cuelists))
        self.active_lists = cuelists
        return cuelists

    @asyncio.coroutine
    def goCueList(self, cuelist):
        yield from self.write('GQL {}'.format(cuelist))
        res = yield from self.read()
        return True

    @asyncio.coroutine
    def releaseCueList(self, cuelist):
        yield from self.write('RQL {}'.format(cuelist))
        res = yield from self.read()
        return True
