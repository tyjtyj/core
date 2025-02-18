"""Sensor for the Austrian "Zentralanstalt für Meteorologie und Geodynamik"."""
import csv
from datetime import datetime, timedelta
import gzip
import json
import logging
import os

from aiohttp.hdrs import USER_AGENT
import requests
import voluptuous as vol

from homeassistant.components.sensor import SensorEntity
from homeassistant.const import (
    AREA_SQUARE_METERS,
    ATTR_ATTRIBUTION,
    CONF_LATITUDE,
    CONF_LONGITUDE,
    CONF_MONITORED_CONDITIONS,
    CONF_NAME,
    DEGREE,
    DEVICE_CLASS_TEMPERATURE,
    LENGTH_METERS,
    PERCENTAGE,
    PRESSURE_HPA,
    SPEED_KILOMETERS_PER_HOUR,
    TEMP_CELSIUS,
    __version__,
)
import homeassistant.helpers.config_validation as cv
from homeassistant.util import Throttle, dt as dt_util

_LOGGER = logging.getLogger(__name__)

ATTR_STATION = "station"
ATTR_UPDATED = "updated"
ATTRIBUTION = "Data provided by ZAMG"

CONF_STATION_ID = "station_id"

DEFAULT_NAME = "zamg"

MIN_TIME_BETWEEN_UPDATES = timedelta(minutes=10)
VIENNA_TIME_ZONE = dt_util.get_time_zone("Europe/Vienna")

SENSOR_TYPES = {
    "pressure": ("Pressure", PRESSURE_HPA, None, "LDstat hPa", float),
    "pressure_sealevel": (
        "Pressure at Sea Level",
        PRESSURE_HPA,
        None,
        "LDred hPa",
        float,
    ),
    "humidity": ("Humidity", PERCENTAGE, None, "RF %", int),
    "wind_speed": (
        "Wind Speed",
        SPEED_KILOMETERS_PER_HOUR,
        None,
        f"WG {SPEED_KILOMETERS_PER_HOUR}",
        float,
    ),
    "wind_bearing": ("Wind Bearing", DEGREE, None, f"WR {DEGREE}", int),
    "wind_max_speed": (
        "Top Wind Speed",
        None,
        SPEED_KILOMETERS_PER_HOUR,
        f"WSG {SPEED_KILOMETERS_PER_HOUR}",
        float,
    ),
    "wind_max_bearing": ("Top Wind Bearing", DEGREE, None, f"WSR {DEGREE}", int),
    "sun_last_hour": ("Sun Last Hour", PERCENTAGE, None, f"SO {PERCENTAGE}", int),
    "temperature": (
        "Temperature",
        TEMP_CELSIUS,
        DEVICE_CLASS_TEMPERATURE,
        f"T {TEMP_CELSIUS}",
        float,
    ),
    "precipitation": (
        "Precipitation",
        None,
        f"l/{AREA_SQUARE_METERS}",
        f"N l/{AREA_SQUARE_METERS}",
        float,
    ),
    "dewpoint": (
        "Dew Point",
        TEMP_CELSIUS,
        DEVICE_CLASS_TEMPERATURE,
        f"TP {TEMP_CELSIUS}",
        float,
    ),
    # The following probably not useful for general consumption,
    # but we need them to fill in internal attributes
    "station_name": ("Station Name", None, None, "Name", str),
    "station_elevation": (
        "Station Elevation",
        LENGTH_METERS,
        None,
        f"Höhe {LENGTH_METERS}",
        int,
    ),
    "update_date": ("Update Date", None, None, "Datum", str),
    "update_time": ("Update Time", None, None, "Zeit", str),
}

PLATFORM_SCHEMA = cv.PLATFORM_SCHEMA.extend(
    {
        vol.Required(CONF_MONITORED_CONDITIONS, default=["temperature"]): vol.All(
            cv.ensure_list, [vol.In(SENSOR_TYPES)]
        ),
        vol.Optional(CONF_STATION_ID): cv.string,
        vol.Optional(CONF_NAME, default=DEFAULT_NAME): cv.string,
        vol.Inclusive(
            CONF_LATITUDE, "coordinates", "Latitude and longitude must exist together"
        ): cv.latitude,
        vol.Inclusive(
            CONF_LONGITUDE, "coordinates", "Latitude and longitude must exist together"
        ): cv.longitude,
    }
)


def setup_platform(hass, config, add_entities, discovery_info=None):
    """Set up the ZAMG sensor platform."""
    name = config.get(CONF_NAME)
    latitude = config.get(CONF_LATITUDE, hass.config.latitude)
    longitude = config.get(CONF_LONGITUDE, hass.config.longitude)

    station_id = config.get(CONF_STATION_ID) or closest_station(
        latitude, longitude, hass.config.config_dir
    )
    if station_id not in _get_ogd_stations():
        _LOGGER.error(
            "Configured ZAMG %s (%s) is not a known station",
            CONF_STATION_ID,
            station_id,
        )
        return False

    probe = ZamgData(station_id=station_id)
    try:
        probe.update()
    except (ValueError, TypeError) as err:
        _LOGGER.error("Received error from ZAMG: %s", err)
        return False

    add_entities(
        [
            ZamgSensor(probe, variable, name)
            for variable in config[CONF_MONITORED_CONDITIONS]
        ],
        True,
    )


class ZamgSensor(SensorEntity):
    """Implementation of a ZAMG sensor."""

    def __init__(self, probe, variable, name):
        """Initialize the sensor."""
        self.probe = probe
        self.client_name = name
        self.variable = variable
        self._attr_device_class = SENSOR_TYPES[variable][2]

    @property
    def name(self):
        """Return the name of the sensor."""
        return f"{self.client_name} {self.variable}"

    @property
    def native_value(self):
        """Return the state of the sensor."""
        return self.probe.get_data(self.variable)

    @property
    def native_unit_of_measurement(self):
        """Return the unit of measurement of this entity, if any."""
        return SENSOR_TYPES[self.variable][1]

    @property
    def extra_state_attributes(self):
        """Return the state attributes."""
        return {
            ATTR_ATTRIBUTION: ATTRIBUTION,
            ATTR_STATION: self.probe.get_data("station_name"),
            ATTR_UPDATED: self.probe.last_update.isoformat(),
        }

    def update(self):
        """Delegate update to probe."""
        self.probe.update()


class ZamgData:
    """The class for handling the data retrieval."""

    API_URL = "http://www.zamg.ac.at/ogd/"
    API_HEADERS = {USER_AGENT: f"home-assistant.zamg/ {__version__}"}

    def __init__(self, station_id):
        """Initialize the probe."""
        self._station_id = station_id
        self.data = {}

    @property
    def last_update(self):
        """Return the timestamp of the most recent data."""
        date, time = self.data.get("update_date"), self.data.get("update_time")
        if date is not None and time is not None:
            return datetime.strptime(date + time, "%d-%m-%Y%H:%M").replace(
                tzinfo=VIENNA_TIME_ZONE
            )

    @classmethod
    def current_observations(cls):
        """Fetch the latest CSV data."""
        try:
            response = requests.get(cls.API_URL, headers=cls.API_HEADERS, timeout=15)
            response.raise_for_status()
            response.encoding = "UTF8"
            return csv.DictReader(
                response.text.splitlines(), delimiter=";", quotechar='"'
            )
        except requests.exceptions.HTTPError:
            _LOGGER.error("While fetching data")

    @Throttle(MIN_TIME_BETWEEN_UPDATES)
    def update(self):
        """Get the latest data from ZAMG."""
        if self.last_update and (
            self.last_update + timedelta(hours=1)
            > datetime.utcnow().replace(tzinfo=dt_util.UTC)
        ):
            return  # Not time to update yet; data is only hourly

        for row in self.current_observations():
            if row.get("Station") == self._station_id:
                api_fields = {
                    col_heading: (standard_name, dtype)
                    for standard_name, (
                        _,
                        _,
                        _,
                        col_heading,
                        dtype,
                    ) in SENSOR_TYPES.items()
                }
                self.data = {
                    api_fields.get(col_heading)[0]: api_fields.get(col_heading)[1](
                        v.replace(",", ".")
                    )
                    for col_heading, v in row.items()
                    if col_heading in api_fields and v
                }
                break
        else:
            raise ValueError(f"No weather data for station {self._station_id}")

    def get_data(self, variable):
        """Get the data."""
        return self.data.get(variable)


def _get_ogd_stations():
    """Return all stations in the OGD dataset."""
    return {r["Station"] for r in ZamgData.current_observations()}


def _get_zamg_stations():
    """Return {CONF_STATION: (lat, lon)} for all stations, for auto-config."""
    capital_stations = _get_ogd_stations()
    req = requests.get(
        "https://www.zamg.ac.at/cms/en/documents/climate/"
        "doc_metnetwork/zamg-observation-points",
        timeout=15,
    )
    stations = {}
    for row in csv.DictReader(req.text.splitlines(), delimiter=";", quotechar='"'):
        if row.get("synnr") in capital_stations:
            try:
                stations[row["synnr"]] = tuple(
                    float(row[coord].replace(",", "."))
                    for coord in ("breite_dezi", "länge_dezi")
                )
            except KeyError:
                _LOGGER.error("ZAMG schema changed again, cannot autodetect station")
    return stations


def zamg_stations(cache_dir):
    """Return {CONF_STATION: (lat, lon)} for all stations, for auto-config.

    Results from internet requests are cached as compressed json, making
    subsequent calls very much faster.
    """
    cache_file = os.path.join(cache_dir, ".zamg-stations.json.gz")
    if not os.path.isfile(cache_file):
        stations = _get_zamg_stations()
        with gzip.open(cache_file, "wt") as cache:
            json.dump(stations, cache, sort_keys=True)
        return stations
    with gzip.open(cache_file, "rt") as cache:
        return {k: tuple(v) for k, v in json.load(cache).items()}


def closest_station(lat, lon, cache_dir):
    """Return the ZONE_ID.WMO_ID of the closest station to our lat/lon."""
    if lat is None or lon is None or not os.path.isdir(cache_dir):
        return
    stations = zamg_stations(cache_dir)

    def comparable_dist(zamg_id):
        """Calculate the pseudo-distance from lat/lon."""
        station_lat, station_lon = stations[zamg_id]
        return (lat - station_lat) ** 2 + (lon - station_lon) ** 2

    return min(stations, key=comparable_dist)
