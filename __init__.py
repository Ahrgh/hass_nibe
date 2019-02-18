"""
Support for nibe uplink.
"""


from datetime import timedelta
import logging
import asyncio
import json
import voluptuous as vol
from typing import (List, Iterable)
from collections import defaultdict
import homeassistant.helpers.config_validation as cv

from homeassistant import config_entries
from homeassistant.core import split_entity_id
from homeassistant.components.group import (
    ATTR_ADD_ENTITIES, ATTR_OBJECT_ID,
    DOMAIN as DOMAIN_GROUP, SERVICE_SET)
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.components import persistent_notification

from .const import *
from .config import NibeConfigFlow  # noqa

_LOGGER = logging.getLogger(__name__)

config_entries.FLOWS.append(DOMAIN)

INTERVAL            = timedelta(minutes=1)

DEPENDENCIES = ['group']
REQUIREMENTS        = ['nibeuplink==0.5.0']


SIGNAL_UPDATE       = 'nibe_update'

BINARY_SENSOR_VALUES = ('off', 'on', 'yes', 'no')

UNIT_SCHEMA = vol.Schema({
    vol.Required(CONF_UNIT): cv.positive_int,
    vol.Optional(CONF_CATEGORIES): vol.All(cv.ensure_list, [cv.string]),
    vol.Optional(CONF_STATUSES): vol.All(cv.ensure_list, [cv.string]),
})

SYSTEM_SCHEMA = vol.Schema({
    vol.Required(CONF_SYSTEM): cv.positive_int,
    vol.Optional(CONF_UNITS, default=[]):
        vol.All(cv.ensure_list, [UNIT_SCHEMA]),
    vol.Optional(CONF_SENSORS, default=[]): vol.All(cv.ensure_list, [cv.string]),
    vol.Optional(CONF_CLIMATES): vol.All(cv.ensure_list, [cv.string]),
    vol.Optional(CONF_WATER_HEATERS): vol.All(cv.ensure_list, [cv.string]),
    vol.Optional(CONF_SWITCHES, default=[]): vol.All(cv.ensure_list, [cv.string]),
    vol.Optional(CONF_BINARY_SENSORS, default=[]): vol.All(cv.ensure_list, [cv.string]),
})

NIBE_SCHEMA = vol.Schema({
    vol.Optional(CONF_SYSTEMS, default=[]):
        vol.All(cv.ensure_list, [SYSTEM_SCHEMA]),
})

CONFIG_SCHEMA = vol.Schema({
    DOMAIN: NIBE_SCHEMA
}, extra=vol.ALLOW_EXTRA)


async def async_setup_systems(hass, uplink, entry):
    config = hass.data[DATA_NIBE]['config']

    if not len(config.get(CONF_SYSTEMS)):
        systems = await uplink.get_systems()
        msg = json.dumps(systems, indent=1)
        persistent_notification.async_create(hass, 'No systems selected, please configure one system id of:<br/><br/><pre>{}</pre>'.format(msg) , 'Invalid nibe config', 'invalid_config')
        return

    systems = {
        config[CONF_SYSTEM]:
            NibeSystem(hass,
                       uplink,
                       config[CONF_SYSTEM],
                       config,
                       entry.entry_id)
        for config in config.get(CONF_SYSTEMS)
    }

    hass.data[DATA_NIBE]['systems'] = systems
    hass.data[DATA_NIBE]['uplink'] = uplink

    tasks = [system.load() for system in systems.values()]

    await asyncio.gather(*tasks)

    for platform in ('climate', 'switch', 'sensor',
                     'binary_sensor', 'water_heater'):
        hass.async_add_job(hass.config_entries.async_forward_entry_setup(
            entry, platform))


async def async_setup(hass, config):
    """Setup nibe uplink component"""
    hass.data[DATA_NIBE] = {}
    hass.data[DATA_NIBE]['config'] = config[DOMAIN]
    return True


async def async_setup_entry(hass, entry: config_entries.ConfigEntry):
    """Set up an access point from a config entry."""
    _LOGGER.debug("Setup nibe entry")

    from nibeuplink import Uplink

    scope = None
    if entry.data.get(CONF_WRITEACCESS):
        scope = ['READSYSTEM', 'WRITESYSTEM']
    else:
        scope = ['READSYSTEM']

    def access_data_write(data):
        hass.config_entries.async_update_entry(
            entry, data={
                **entry.data, CONF_ACCESS_DATA: data
            })

    uplink = Uplink(
        client_id = entry.data.get(CONF_CLIENT_ID),
        client_secret = entry.data.get(CONF_CLIENT_SECRET),
        redirect_uri = entry.data.get(CONF_REDIRECT_URI),
        access_data = entry.data.get(CONF_ACCESS_DATA),
        access_data_write = access_data_write,
        scope = scope
    )

    await uplink.refresh_access_token()

    await async_setup_systems(hass, uplink, entry)

    return True


async def async_unload_entry(hass, entry):
    pass


def filter_list(data: List[dict], field: str, selected: List[str]):
    """Return a filtered array based on existance in a filter list"""
    if len(selected):
        return [x for x in data if x[field] in selected]
    else:
        return data


def gen_dict():
    return {'groups': [], 'data': None}


class NibeSystem(object):
    def __init__(self, hass, uplink, system_id, config, entry_id):
        self.hass = hass
        self.parameters = {}
        self.config = config
        self.system_id = system_id
        self.entry_id = entry_id
        self.system = None
        self.uplink = uplink
        self.notice = []
        self.sensors = defaultdict(gen_dict)
        self.binary_sensors = defaultdict(gen_dict)
        self._device_info = {}

    @property
    def device_info(self):
        """Return a device description for device registry."""
        return self._device_info

    async def load_parameter_group(self,
                                   name: str,
                                   object_id: str,
                                   parameters: List[dict]):

        group = self.hass.components.group
        entity = await group.Group.async_create_group(
            self.hass,
            name=name,
            control=False,
            object_id='{}_{}_{}'.format(DOMAIN, self.system_id, object_id))

        _, group_id = split_entity_id(entity.entity_id)

        for x in parameters:
            if str(x['value']).lower() in BINARY_SENSOR_VALUES:
                list_object = self.binary_sensors
            else:
                list_object = self.sensors

            entry = list_object[x['parameterId']]
            entry['data'] = x
            entry['groups'].append(group_id)
            _LOGGER.debug("Entry {}".format(entry))

        return entity.entity_id

    async def load_categories(self,
                              unit: int,
                              selected):
        data = await self.uplink.get_categories(self.system_id, True, unit)
        data = filter_list(data, 'categoryId', selected)
        tasks = [
            self.load_parameter_group(
                x['name'],
                '{}_{}'.format(unit, x['categoryId']),
                x['parameters'])
            for x in data
        ]
        await asyncio.gather(*tasks)

    async def load_status(self,
                          unit: int):
        data = await self.uplink.get_unit_status(self.system_id, unit)
        tasks = [
            self.load_parameter_group(
                x['title'],
                '{}_{}'.format(unit, x['title']),
                x['parameters'])
            for x in data
        ]
        await asyncio.gather(*tasks)

    async def load_unit(self, unit):

        tasks = []

        if CONF_CATEGORIES in unit:
            tasks.append(self.load_categories(
                unit.get(CONF_UNIT),
                unit.get(CONF_CATEGORIES)))

        if CONF_STATUSES in unit:
            tasks.append(self.load_status(
                unit.get(CONF_UNIT)))

        await asyncio.gather(*tasks)

    async def load(self):
        self.system = await self.uplink.get_system(self.system_id)
        _LOGGER.debug("Loading system: {}".format(self.system))

        self._device_info = {
            'identifiers': {(DOMAIN, self.system_id)},
            'manufacturer': "NIBE Energy Systems",
            'model': self.system.get('productName'),
            'name': self.system.get('name'),
        }

        device_registry = await \
            self.hass.helpers.device_registry.async_get_registry()
        device_registry.async_get_or_create(
            config_entry_id=self.entry_id,
            **self._device_info
        )

        tasks = []

        for unit in self.config.get(CONF_UNITS):
            tasks.append(self.load_unit(unit))

        await asyncio.gather(*tasks)

        await self.update()
        async_track_time_interval(self.hass, self.update, INTERVAL)

    async def update(self, now=None):
        notice = await self.uplink.get_notifications(self.system_id)
        added = [k for k in notice if k not in self.notice]
        removed = [k for k in self.notice if k not in notice]
        self.notice = notice

        for x in added:
            persistent_notification.async_create(
                self.hass,
                x['info']['description'],
                x['info']['title'],
                'nibe:{}'.format(x['notificationId'])
            )
        for x in removed:
            persistent_notification.async_dismiss(
                'nibe:{}'.format(x['notificationId'])
            )
