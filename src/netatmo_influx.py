#!/usr/bin/env python3
# encoding=utf-8

import argparse
import logging
import signal
import sys
from configparser import ConfigParser
from datetime import datetime, UTC
from os import getenv
from pathlib import Path
from time import sleep
from typing import Tuple, Optional

import pyatmo.helpers
from influxdb_client import InfluxDBClient, WritePrecision
from influxdb_client.client.exceptions import InfluxDBError
from oauthlib.oauth2.rfc6749.errors import InvalidGrantError
from pyatmo import NetatmoOAuth2, WeatherStationData, ApiError
from requests import ConnectionError


class BatchingCallback(object):
    @staticmethod
    def success(conf: (str, str, str), data: str):
        log.info(f"Written batch with size {len(data)}.")
        if influx_debug:
            log.debug(f"Batch: {conf}, Data: {data}")

    @staticmethod
    def error(conf: (str, str, str), data: str, exception: InfluxDBError):
        if type(exception) is InfluxDBError and exception.response is not None:
            log.error(f"Cannot write batch due: {exception.response.status} - {exception.response.reason}")
        else:
            log.error(f"Cannot write batch due: {exception}")
        if influx_debug:
            log.debug(f"Batch: {conf}, Data: {data}, Exception: {exception}")

    @staticmethod
    def retry(conf: (str, str, str), data: str, exception: InfluxDBError):
        if type(exception) is InfluxDBError and exception.response is not None:
            log.warning(
                f"Retryable error occurs for batch, retry: {exception.response.status} - {exception.response.reason}"
            )
        else:
            log.warning(f"Retryable error occurs for batch, retry: {exception}")
        if influx_debug:
            log.debug(f"Batch: {conf}, Data: {data}, Exception: {exception}")


def parse_config(_config_file: str = None) -> Tuple[ConfigParser, str]:
    _config = ConfigParser(interpolation=None)

    if _config_file is None:
        _config_file = Path(__file__).parent / "config.ini"

    if _config_file.exists():
        _config.read(_config_file)

    return _config, _config_file


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("-f", "--file", dest="config_file", type=str, nargs=1, required=False)
    parser.add_argument("-v", "--verbose", dest="verbosity", action="count", default=0)

    return parser.parse_args()


def set_logging_level(_verbosity, _level, _logger=None):
    _switcher = {
        1: "WARNING",
        2: "INFO",
        3: "DEBUG",
    }
    if _verbosity > 0:
        _level = _switcher.get(_verbosity)

    _fmt = logging.Formatter(
        "%(asctime)s - %(module)s:%(lineno)d - %(levelname)s:%(message)s", datefmt="%d.%m.%Y %H:%M:%S"
    )

    # Basic Setting for Debugging
    pyatmo.helpers.LOG.setLevel(_level)

    # Logger
    if _logger is None:
        _logger = logging.getLogger(__name__)

    _ch = logging.StreamHandler()
    _ch.setFormatter(_fmt)

    _logger.addHandler(_ch)
    _logger.setLevel(_level)
    _logger.info(f"Setting loglevel to {_level}.")

    return _logger


def shutdown(_signal):
    global running
    running = False


def get_authorization(
    _client_id: str, _client_secret: str, _refresh_token: str, _token_expiration: float = 0
) -> Tuple[NetatmoOAuth2, str, float]:
    while True:
        try:
            _auth = NetatmoOAuth2(
                client_id=_client_id,
                client_secret=_client_secret,
            )
            _auth.extra["refresh_token"] = _refresh_token
            _result = _auth.refresh_tokens()
            _refresh_token = _result.get("refresh_token")

            override = ConfigParser(interpolation=None)
            override["netatmo"] = {"refresh_token": _refresh_token}
            with open(config_file, "w") as f:
                if "netatmo" in config:
                    config["netatmo"]["refresh_token"] = _refresh_token
                    config.write(f)
                else:
                    override.write(f)

            return _auth, _refresh_token, _token_expiration
        except ApiError:
            log.error("No credentials supplied. No Netatmo Account available.")
            exit(1)
        except ConnectionError:
            log.error(f"Can't connect to Netatmo API. Retrying in {interval} second(s)...")
            pass
        except InvalidGrantError:
            log.error("Refresh Token expired!")
            exit(1)


def safe_list_get(_input_list: list, _idx: int, _default=None) -> Optional[str | int | float]:
    try:
        return _input_list[_idx]
    except IndexError:
        return _default


def get_sensor_data(_sensor_data: dict, _station_name: str, _module_name: str, _module_type: str) -> list:
    _measurements = []
    _date_times = {}

    if _sensor_data is not None:
        _time = _sensor_data.pop("time_utc")
        # for the first 5 minutes of each day there is no max* data for neither wind nor temperature sensors
        if _module_type == "NAModule2" and "max_wind_str" in _sensor_data:
            _date_times = {"date_max_wind_str": _sensor_data.pop("date_max_wind_str")}
        if _module_type not in ["NAModule3", "NAModule2"] and all(k in _sensor_data for k in ("max_temp", "min_temp")):
            _date_times = {
                "date_max_temp": _sensor_data.pop("date_max_temp"),
                "date_min_temp": _sensor_data.pop("date_min_temp"),
            }
        for _sensor, _value in _sensor_data.items():
            _measurements.append(
                {
                    "measurement": _sensor.lower() if _sensor.lower() != "wifi_status" else "rf_status",
                    "tags": {"station": _station_name, "module": _module_name, "type": _module_type},
                    "fields": {"value": check_value(_value)},
                    "time": (
                        _time
                        if _sensor not in ["max_temp", "min_temp", "max_wind_str"]
                        else _date_times.get(f"date_{_sensor}")
                    ),
                }
            )
    return _measurements


def check_value(_val: [float | int | str]) -> [float | str]:
    if type(_val) is int:
        return float(_val)
    return _val


if __name__ == "__main__":
    running = True
    interval = None
    loglevel = None
    debug_batch = False
    client_id = None
    client_secret = None
    refresh_token = None
    token_expiration = 0
    influx_host = None
    influx_port = None
    influx_bucket = None
    influx_protocol = None
    influx_token = None
    influx_org = None
    influx_debug = False
    influx_callback = BatchingCallback()
    args = parse_args()
    config, config_file = parse_config(args.config_file)

    if getenv("TERM", None):
        # noinspection PyTypeChecker
        signal.signal(signal.SIGTERM, shutdown)
        # noinspection PyTypeChecker
        signal.signal(signal.SIGINT, shutdown)

    if "global" in config:
        interval = int(config["global"].get("interval", "300"))  # interval in seconds; default are 5 Minutes
        loglevel = config["global"].get("loglevel", "INFO")  # set loglevel by Name
        debug_batch = config["global"].get("debug_batch", "False")  # set loglevel for batching (influx)

    if "netatmo" in config:
        client_id = config["netatmo"].get("client_id", None)
        client_secret = config["netatmo"].get("client_secret", None)

    if "influx" in config:
        influx_host = config["influx"].get("influx_host", "localhost")
        influx_port = config["influx"].get("influx_port", "8086")
        influx_bucket = config["influx"].get("influx_bucket", "netatmo")
        influx_protocol = config["influx"].get("influx_protocol", "http")
        influx_token = config["influx"].get("influx_token", None)
        influx_org = config["influx"].get("influx_org", None)

    # Environment Variables takes precedence over config if set
    # global
    interval = int(getenv("INTERVAL", interval))
    loglevel = getenv("LOGLEVEL", loglevel)
    debug_batch = getenv("DEBUG_BATCH", debug_batch)
    # netatmo
    client_id = getenv("NETATMO_CLIENT_ID", client_id)
    client_secret = getenv("NETATMO_CLIENT_SECRET", client_secret)
    # refresh_token needs to be persisted in the config, but can be set as env var for first run
    refresh_token = config.get("netatmo", "refresh_token", fallback=getenv("NETATMO_REFRESH_TOKEN"))
    # influx
    influx_host = getenv("INFLUX_HOST", influx_host)
    influx_port = getenv("INFLUX_PORT", influx_port)
    influx_bucket = getenv("INFLUX_BUCKET", influx_bucket)
    influx_protocol = getenv("INFLUX_PROTOCOL", influx_protocol)
    influx_token = getenv("INFLUX_TOKEN", influx_token)
    influx_org = getenv("INFLUX_ORG", influx_org)

    # set logging level
    log = set_logging_level(args.verbosity, loglevel)
    if (loglevel == "DEBUG" or args.verbosity == 3) and debug_batch == "True":
        influx_debug = True

    log.info("Starting Netatmo Crawler...")
    while running:
        authorization, refresh_token, token_expiration = get_authorization(
            client_id, client_secret, refresh_token, token_expiration
        )
        try:
            weatherData = WeatherStationData(authorization)
            weatherData.update()

            with InfluxDBClient(
                url=f"{influx_protocol}://{influx_host}:{influx_port}",
                token=influx_token,
                org=influx_org,
                debug=influx_debug,
            ) as client:
                for _, logger in client.conf.loggers.items():
                    logger.setLevel(logging.NOTSET)
                    logger.addHandler(logging.StreamHandler(sys.stderr))

                with client.write_api(
                    success_callback=influx_callback.success,
                    error_callback=influx_callback.error,
                    retry_callback=influx_callback.retry,
                ) as write_client:
                    for station in weatherData.stations.values():
                        measurements = []

                        log.debug(f"Station Data: {station}")
                        station_name = station.get("home_name", "Unknown")
                        station_module_name = station.get("module_name", "Unknown")
                        station_module_type = station.get("type", "Unknown")
                        station_place = station.get("place", {})
                        station_long_lat = station_place.get("location", [])

                        station_data = {
                            "altitude": station_place.get("altitude"),
                            "country": station_place.get("country"),
                            "timezone": station_place.get("timezone"),
                            "longitude": safe_list_get(station_long_lat, 0),
                            "latitude": safe_list_get(station_long_lat, 1),
                        }

                        for key, value in station_data.items():
                            measurements.append(
                                {
                                    "measurement": key,
                                    "tags": {"station": station_name, "type": station_module_type},
                                    "fields": {"value": check_value(value)},
                                    "time": int(datetime.now(UTC).timestamp()),
                                }
                            )

                        station_sensor_data = station.get("dashboard_data")

                        if station_sensor_data is None:
                            continue

                        for sensor in ["wifi_status", "reachable", "co2_calibrating"]:
                            station_sensor_data.update({sensor: station.get(sensor)})

                        measurements += get_sensor_data(
                            station_sensor_data, station_name, station_module_name, station_module_type
                        )

                        for module in station.get("modules"):
                            log.debug(f"Module Data: {module}")
                            module_name = module.get("module_name")
                            module_type = module.get("type")

                            module_sensor_data = module.get("dashboard_data")

                            if module_sensor_data is None:
                                continue

                            for sensor in ["rf_status", "battery_vp", "battery_percent"]:
                                module_sensor_data.update({sensor: module.get(sensor)})

                            measurements += get_sensor_data(module_sensor_data, station_name, module_name, module_type)

                        # noinspection PyTypeChecker
                        write_client.write(
                            bucket=influx_bucket, org=influx_org, record=measurements, write_precision=WritePrecision.S
                        )
        except ApiError as error:
            log.error(error)
            pass

        sleep(interval)
