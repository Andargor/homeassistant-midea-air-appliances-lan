"""
The custom component for local network access to Midea appliances
"""

from __future__ import annotations
import asyncio

from datetime import timedelta
import itertools
import logging
from typing import Any, Final, Iterator, Tuple, cast, final

from homeassistant.components.network import async_get_ipv4_broadcast_addresses
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    CONF_API_VERSION,
    CONF_BROADCAST_ADDRESS,
    CONF_DEVICES,
    CONF_DISCOVERY,
    CONF_ID,
    CONF_IP_ADDRESS,
    CONF_NAME,
    CONF_PASSWORD,
    CONF_SCAN_INTERVAL,
    CONF_TOKEN,
    CONF_TYPE,
    CONF_UNIQUE_ID,
    CONF_USERNAME,
)
from homeassistant.core import HomeAssistant, callback, CALLBACK_TYPE
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.debounce import Debouncer
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
    UpdateFailed,
)
from homeassistant.util import slugify


from midea_beautiful.appliance import AirConditionerAppliance, DehumidifierAppliance
from midea_beautiful.exceptions import AuthenticationError, MideaError
from midea_beautiful.lan import LanDevice

from custom_components.midea_dehumidifier_lan.api import (
    MideaClient,
    is_climate,
    is_dehumidifier,
    supported_appliance,
)
from custom_components.midea_dehumidifier_lan.const import (
    APPLIANCE_REFRESH_COOLDOWN,
    APPLIANCE_REFRESH_INTERVAL,
    APPLIANCE_SCAN_INTERVAL,
    CONF_APPID,
    CONF_APPKEY,
    CONF_TOKEN_KEY,
    DEFAULT_DISCOVERY_MODE,
    DISCOVERY_CLOUD,
    DISCOVERY_WAIT,
    DISCOVERY_IGNORE,
    DISCOVERY_LAN,
    UNKNOWN_IP,
    UNIQUE_DEHUMIDIFIER_PREFIX,
    DOMAIN,
)


_LOGGER = logging.getLogger(__name__)

_NUMBER_OF_BROADCASTS: Final = 64


ADDITIONAL_ADDRESSES = iter(
    [
        [],
        ["10.0.4.210"],
        ["10.0.7.130"],
        ["10.0.7.130"],
        ["10.0.7.130"],
        [],
    ]
)


class Hub:  # pylint: disable=too-few-public-methods,too-many-instance-attributes
    """Central class for interacting with appliances"""

    def __init__(self, hass: HomeAssistant, config_entry: ConfigEntry) -> None:
        self.coordinators: list[ApplianceUpdateCoordinator] = []

        self.cloud = None
        self.hass = hass
        self.config_entry = config_entry
        self.data = {}
        self.client = MideaClient()
        self.errors = []
        self.failed_setup = []
        self._updated_conf = False
        self.remove_discovery: CALLBACK_TYPE | None = None
        self.address_iterator: Iterator[list[str]] = iter([])
        self.broadcast_addresses: list[str] = []

    async def _setup_discovery(self) -> None:
        """Startup-time setup"""

        if self.remove_discovery:
            self.remove_discovery()
            self.remove_discovery = None

        self.broadcast_addresses: list[str] = self.data.get(CONF_BROADCAST_ADDRESS, [])
        _LOGGER.debug("Using broadcast address %s", self.broadcast_addresses)
        has_waiting = any(
            device
            for device in self.data[CONF_DEVICES]
            if device[CONF_DISCOVERY] == DISCOVERY_WAIT
        )
        if has_waiting and self.broadcast_addresses:

            def ip_iteration():

                for addr in self.broadcast_addresses:
                    if addr == "255.255.255.255":
                        continue
                    if addr[-4:] == ".255":
                        base = addr[:-3]
                        _LOGGER.debug("Scanning IPv4 addresses %s0-%s254", base, base)
                        for i in range(0, 254, _NUMBER_OF_BROADCASTS):
                            yield [
                                base + str(j)
                                for j in range(i, i + _NUMBER_OF_BROADCASTS)
                            ]

            self.address_iterator = itertools.cycle(ip_iteration())
        else:
            self.address_iterator = iter([])
        scan_interval = self.data.get(CONF_SCAN_INTERVAL, APPLIANCE_SCAN_INTERVAL)
        _LOGGER.debug("Scan interval %s minute(s)", scan_interval)
        if scan_interval:
            self.remove_discovery = async_track_time_interval(
                self.hass, self._async_discover, timedelta(minutes=scan_interval)
            )

    async def async_setup(self) -> None:
        """
        Sets up appliances and creates an update coordinator for
        each one
        """
        self.data = {**self.config_entry.data}
        self.errors = {}
        self._updated_conf = False

        devices = []
        for device_conf in self.data[CONF_DEVICES]:
            device = {**device_conf}
            await self._process_appliance(device)
            devices.append(device)
        self.data[CONF_DEVICES] = devices

        for coordinator in self.coordinators:
            await coordinator.async_config_entry_first_refresh()

        if self._updated_conf:
            self.hass.config_entries.async_update_entry(
                self.config_entry, data=self.data
            )

        await self._setup_discovery()

        if self.errors:
            raise ConfigEntryNotReady(str(self.errors))

    async def _async_discover(self, *_: Any) -> None:
        """Discover Midea Appliances on configured network interfaces."""
        addresses = []
        addresses.extend([str(address) for address in self.broadcast_addresses])
        if new_addresses := next(self.address_iterator, None):
            addresses.extend(new_addresses)
        if not addresses:
            iface_broadcast = await async_get_ipv4_broadcast_addresses(self.hass)
            addresses.extend([str(address) for address in iface_broadcast])
        _LOGGER.debug("Initiated discovery via %s", addresses)
        if result := self.client.find_appliances(
            addresses=addresses, cloud=self.cloud, retries=1
        ):
            await self._async_trigger_discovery(result)

    async def _async_trigger_discovery(self, devices: list[LanDevice]) -> None:
        """Trigger config flows for discovered devices."""

        conf = self.data
        dev_confs: list[dict] = []
        for dev_data in conf[CONF_DEVICES]:
            dev_conf = {**dev_data}
            dev_conf.setdefault(CONF_DISCOVERY, DEFAULT_DISCOVERY_MODE)
            dev_conf.setdefault(CONF_IP_ADDRESS, UNKNOWN_IP)
            dev_confs.append(dev_conf)
        conf[CONF_DEVICES] = dev_confs
        new_devices: list[LanDevice] = []
        changed_devices: list[Tuple[LanDevice, ApplianceUpdateCoordinator]] = []

        self._iterate_devices(devices, conf, new_devices, changed_devices)

        need_reload = False
        updated_conf = False
        for new in new_devices:
            for known in dev_confs:
                if known[CONF_UNIQUE_ID] == new.serial_number:
                    if known[CONF_DISCOVERY] == DISCOVERY_WAIT:
                        update = {
                            CONF_DISCOVERY: DISCOVERY_LAN,
                            CONF_API_VERSION: new.version,
                            CONF_ID: new.appliance_id,
                            CONF_IP_ADDRESS: new.address,
                            CONF_NAME: new.name,
                            CONF_TOKEN_KEY: new.key,
                            CONF_TOKEN: new.token,
                            CONF_TYPE: new.type,
                            CONF_UNIQUE_ID: new.serial_number,
                        }
                        _LOGGER.warning("Updating discovered device %s", new)
                        _LOGGER.warning("Updating old value %s", known)
                        _LOGGER.warning("New value %s", update)

                        known.update(update)
                        updated_conf = True
                        need_reload = True
                    if known[CONF_DISCOVERY] != DISCOVERY_LAN:
                        _LOGGER.warning(
                            "Device %s in discovery mode %s found on address %s."
                            " It can be configured for LAN access.",
                            new,
                            known[CONF_DISCOVERY],
                            new.address,
                        )
                    break
        for changed in changed_devices:
            for known in dev_confs:
                if known[CONF_UNIQUE_ID] == changed[1].appliance.serial_number:
                    if known[CONF_DISCOVERY] == DISCOVERY_LAN:
                        changed[1].appliance.address = changed[0].address
                        known[CONF_IP_ADDRESS] = changed[0].address
                        updated_conf = True
                        break
                    _LOGGER.warning(
                        "Device %s in discovery mode %s found on address %s."
                        " It can be configured for LAN access.",
                        changed[1].appliance,
                        changed[1].discovery_mode,
                        changed[0].address,
                    )

        if updated_conf:
            self.hass.config_entries.async_update_entry(
                entry=self.config_entry,
                data=self.data,
            )
        if need_reload:
            self.hass.async_create_task(
                self.hass.config_entries.async_reload(self.config_entry.entry_id)
            )

    def _iterate_devices(self, devices, conf, new_devices, changed_devices):
        for device in devices:
            existing = next(
                (
                    coord
                    for coord in self.coordinators
                    if coord.appliance.serial_number == device.serial_number
                ),
                None,
            )
            if existing:
                # If address changed, we need to handle it
                if device.address != existing.appliance.address:
                    _LOGGER.warning("DEV: Discovered changed device %r", device)
                    changed_devices.append((device, existing))
            else:
                _LOGGER.warning("DEV: Discovered device %r", device)
                if supported_appliance(conf, device):
                    new_devices.append(device)
                else:
                    _LOGGER.debug("DEV: Ignored device %s", device)
        _LOGGER.debug("Changed devices: %s", changed_devices)
        _LOGGER.debug("Discovered new devices: %s", new_devices)

    async def _process_appliance(self, device: dict):
        _LOGGER.debug("conf=%s", self.data)
        _LOGGER.debug("device=%s", device)
        discovery_mode = device.get(CONF_DISCOVERY)
        # We are waiting for appliance to come online
        if discovery_mode == DISCOVERY_IGNORE:
            _LOGGER.debug("Ignored appliance for discovery %s", device)
            return
        if discovery_mode == DISCOVERY_WAIT:
            _LOGGER.debug("Waiting for appliance discovery %s", device)
            return
        use_cloud = discovery_mode == DISCOVERY_CLOUD
        need_cloud = use_cloud
        lan_mode = discovery_mode == DISCOVERY_LAN
        version = device.get(CONF_API_VERSION, 3)
        need_token = (
            discovery_mode == DISCOVERY_LAN
            and version >= 3
            and (not device.get(CONF_TOKEN) or not device.get(CONF_TOKEN_KEY))
        )
        if need_token:
            _LOGGER.warning(
                "Appliance %s, id=%s has no token,"
                " trying to obtain it from Midea cloud API",
                device.get(CONF_NAME),
                device.get(CONF_ID),
            )
            need_cloud = True
        if need_cloud and self.cloud is None:
            try:
                self.cloud = await self.hass.async_add_executor_job(
                    self.client.connect_to_cloud,
                    self.data[CONF_USERNAME],
                    self.data[CONF_PASSWORD],
                    self.data[CONF_APPKEY],
                    self.data[CONF_APPID],
                )
            except AuthenticationError as ex:
                raise ConfigEntryAuthFailed(
                    f"Unable to login to Midea cloud {ex}"
                ) from ex
            except Exception as ex:  # pylint: disable=broad-except
                self.errors[device[CONF_UNIQUE_ID]] = str(ex)
                return

        try:
            appliance = await self.hass.async_add_executor_job(
                self.client.appliance_state,
                device[CONF_IP_ADDRESS] if lan_mode else None,  # ip
                device.get(CONF_TOKEN),  # token
                device.get(CONF_TOKEN_KEY),  # key
                self.cloud,  # cloud
                use_cloud,  # use_cloud
                device[CONF_ID],  # id
            )
            if appliance is not None:
                self._create_coordinator(appliance, device, need_token)
        except Exception as ex:  # pylint: disable=broad-except
            self.errors[device[CONF_UNIQUE_ID]] = str(ex)
            _LOGGER.error(
                "Error while setting appliance id=%s sn=%s ip=%s: %s",
                device[CONF_ID],
                device[CONF_UNIQUE_ID],
                device[CONF_IP_ADDRESS],
                ex,
                exc_info=True,
            )

    def _create_coordinator(self, appliance: LanDevice, device: dict, need_token: bool):
        appliance.name = device[CONF_NAME]
        if not device.get(CONF_API_VERSION):
            device[CONF_API_VERSION] = appliance.version
            self._updated_conf = True
            _LOGGER.debug("Updating version for %s", appliance)
        if need_token and appliance.token and appliance.key:
            device[CONF_TOKEN] = appliance.token
            device[CONF_TOKEN_KEY] = appliance.key
            self._updated_conf = True
            _LOGGER.debug("Updating token for %s", appliance)
        coordinator = ApplianceUpdateCoordinator(self.hass, self, appliance, device)
        self.coordinators.append(coordinator)


class ApplianceUpdateCoordinator(DataUpdateCoordinator):
    """Single class to retrieve data from an appliance"""

    def __init__(
        self,
        hass: HomeAssistant,
        hub: Hub,
        appliance: LanDevice,
        config: dict[str, Any],
    ):
        super().__init__(
            hass,
            _LOGGER,
            name=appliance.name,
            update_method=self._async_appliance_refresh,
            update_interval=timedelta(seconds=APPLIANCE_REFRESH_INTERVAL),
            request_refresh_debouncer=Debouncer(
                hass,
                _LOGGER,
                cooldown=APPLIANCE_REFRESH_COOLDOWN,
                immediate=True,
                function=self.async_refresh,
            ),
        )
        self.hub = hub
        self.appliance = appliance
        self.updating = {}
        self.wait_for_update = False
        self.discovery_mode = config.get(CONF_DISCOVERY, DISCOVERY_IGNORE)
        self.use_cloud: bool = self.discovery_mode == DISCOVERY_CLOUD

    def _cloud(self):
        if self.use_cloud:
            if not self.hub.cloud:
                raise UpdateFailed("Midea cloud API was not initialized")
            return self.hub.cloud
        return None

    async def _async_appliance_refresh(self) -> LanDevice:
        """Called to refresh appliance state"""

        if self.wait_for_update:
            return self.appliance
        try:
            if self.updating:
                self.wait_for_update = True
                _LOGGER.debug(
                    "Updating attributes for %s: %s for", self.appliance, self.updating
                )
                for attr in self.updating:
                    setattr(self.appliance.state, attr, self.updating[attr])
                self.updating = {}
                await self.hass.async_add_executor_job(
                    self.appliance.apply, self._cloud()
                )

            _LOGGER.debug("Refreshing %s", self.appliance)

            await self.hass.async_add_executor_job(
                self.appliance.refresh, self._cloud()
            )
        except MideaError as ex:
            raise UpdateFailed(str(ex)) from ex
        finally:
            self.wait_for_update = False
        return self.appliance

    def is_climate(self) -> bool:
        """True if appliance is air conditioner"""
        return is_climate(self.appliance)

    def is_dehumidifier(self) -> bool:
        """True if appliance is dehumidifier"""
        return is_dehumidifier(self.appliance)

    @final
    def dehumidifier(self) -> DehumidifierAppliance:
        """Returns state as dehumidifier"""
        return cast(DehumidifierAppliance, self.appliance.state)

    @final
    def airconditioner(self) -> AirConditionerAppliance:
        """Returns state as air conditioner"""
        return cast(AirConditionerAppliance, self.appliance.state)

    async def async_apply(self, args: dict) -> None:
        """Applies changes to device"""
        for key, value in args.items():
            self.updating[key] = value
        await self.async_request_refresh()


class ApplianceEntity(CoordinatorEntity):
    """Represents an appliance that gets data from a coordinator"""

    _unique_id_prefx = UNIQUE_DEHUMIDIFIER_PREFIX
    _name_suffix = ""

    def __init__(self, coordinator: ApplianceUpdateCoordinator) -> None:
        super().__init__(coordinator)
        self.coordinator = coordinator
        self.appliance = coordinator.appliance
        self._attr_unique_id = f"{self.unique_id_prefix}{self.appliance.serial_number}"
        self._attr_name = str(self.appliance.name or self.unique_id) + self.name_suffix
        self._applying = False

    async def async_added_to_hass(self) -> None:
        """When entity is added to hass."""
        await super().async_added_to_hass()
        self.async_on_remove(self.coordinator.async_add_listener(self._updated_data))

    @callback
    def _updated_data(self):
        """Called when data has been updated by coordinator"""
        self.appliance = self.coordinator.appliance
        self._attr_available = self.appliance.online
        self.process_update()

    def process_update(self):
        """Allows additional processing after the coordinator updates data"""

    @final
    def dehumidifier(self) -> DehumidifierAppliance:
        """Returns state as dehumidifier"""
        return cast(DehumidifierAppliance, self.appliance.state)

    @final
    def airconditioner(self) -> AirConditionerAppliance:
        """Returns state as air conditioner"""
        return cast(AirConditionerAppliance, self.appliance.state)

    @property
    def name_suffix(self) -> str:
        """Suffix to append to entity name"""
        return self._name_suffix

    @property
    def unique_id_prefix(self) -> str:
        """Prefix for entity id"""
        strip = self.name_suffix.strip()
        if len(strip) == 0:
            return self._unique_id_prefx
        slug = slugify(strip)
        return f"{self._unique_id_prefx}{slug}_"

    @property
    def device_info(self) -> DeviceInfo:
        identifier = str(self.appliance.serial_number or self.appliance.serial_number)
        return DeviceInfo(
            identifiers={(DOMAIN, str(identifier))},
            name=self.appliance.name,
            manufacturer="Midea",
            model=str(self.appliance.model),
            sw_version=self.appliance.firmware_version,
        )

    def apply(self, *args, **kwargs) -> None:
        """Applies changes to device"""
        if len(args) % 2 != 0:
            raise ValueError(f"Expecting attribute/value pairs, had {len(args)} items")
        aargs = {}
        for i in range(0, len(args), 2):
            aargs[args[i]] = args[i + 1]
        for key, value in kwargs.items():
            aargs[key] = value
        asyncio.run_coroutine_threadsafe(
            self.coordinator.async_apply(aargs), self.hass.loop
        ).result()
