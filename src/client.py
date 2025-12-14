#!/usr/bin/env python3
# encoding=utf-8

import argparse
import json
import logging
import signal
from datetime import UTC, datetime
from enum import Enum
from os import getenv
from pathlib import Path
from time import sleep
from typing import Dict, Optional, Union

import requests
import yaml
from influxdb_client import InfluxDBClient, WritePrecision
from influxdb_client.client.exceptions import InfluxDBError

from helpers import configure_logging
from netatmo_api import (
    NetatmoAPI,
    NetatmoAPIError,
    NetatmoAuthError,
    NetatmoThrottlingError,
)


class VerbosityLevel(Enum):
    NOTSET = 0
    WARNING = 1
    INFO = 2
    DEBUG = 3


class BatchingCallback(object):
    @staticmethod
    def success(conf: tuple[str, str, str], data: str):
        log.info(f"Written batch with size {len(data)}.")
        if debug_batch:
            log.debug(f"Batch: {conf}, Data: {data}")

    @staticmethod
    def error(conf: tuple[str, str, str], data: str, exception: InfluxDBError):
        if type(exception) is InfluxDBError and exception.response is not None:
            log.error(f"Cannot write batch due: {exception.response.status} - {exception.response.reason}")
        else:
            log.error(f"Cannot write batch due: {exception}")
        if debug_batch:
            log.debug(f"Batch: {conf}, Data: {data}, Exception: {exception}")

    @staticmethod
    def retry(conf: tuple[str, str, str], data: str, exception: InfluxDBError):
        if type(exception) is InfluxDBError and exception.response is not None:
            log.warning(
                f"Retryable error occurs for batch, retry: {exception.response.status} - {exception.response.reason}"
            )
        else:
            log.warning(f"Retryable error occurs for batch, retry: {exception}")
        if debug_batch:
            log.debug(f"Batch: {conf}, Data: {data}, Exception: {exception}")


def parse_config(_config_file=None) -> Dict:
    if _config_file is None:
        _config_file = Path(__file__).parent / "config.yaml"

    try:
        with open(_config_file, "r", encoding="utf-8") as _f:
            _config = yaml.safe_load(_f)
    except FileNotFoundError:
        return {}
    except yaml.YAMLError as _error:
        if hasattr(_error, "problem_mark"):
            _mark = _error.problem_mark
            print("Error in configuration")
            print(f"Error position: ({_mark.line + 1}:{_mark.column + 1})")
        exit(1)
    else:
        return _config


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("-c", "--config-file", dest="config_file", type=str, nargs="?", required=False, default=None)
    parser.add_argument("-t", "--token-file", dest="token_file", type=str, nargs="?", default="data/token.json")
    parser.add_argument("-v", "--verbose", dest="verbosity", action="count", default=0)
    parser.add_argument("--debug-batch", dest="debug_batch", action="store_true", default=False)

    return parser.parse_args()


def safe_list_get(_input_list: list, _idx: int, _default=None) -> Optional[str | int | float]:
    try:
        return _input_list[_idx]
    except IndexError:
        return _default


def check_value(_val: Union[float, int, str]) -> Union[float, str]:
    if type(_val) is int:
        return float(_val)
    return _val


def shutdown(_signal):
    global running
    running = False


def get_sensor_data(
    _sensor_data: dict, _home_name: str, _station_name: str, _module_name: str, _module_type: str
) -> list:
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
                    "tags": {
                        "home": _home_name,
                        "station": _station_name,
                        "module": _module_name,
                        "type": _module_type,
                    },
                    "fields": {"value": check_value(_value)},
                    "time": (
                        _time
                        if _sensor not in ["max_temp", "min_temp", "max_wind_str"]
                        else _date_times.get(f"date_{_sensor}")
                    ),
                }
            )
    return _measurements


if __name__ == "__main__":
    running = True
    client_id = None
    client_secret = None
    refresh_token = None
    influx_host = None
    influx_port = None
    influx_bucket = None
    influx_protocol = None
    influx_token = None
    influx_org = None
    influx_callback = BatchingCallback()
    args = parse_args()
    config = parse_config(args.config_file)

    if getenv("TERM", None):
        # noinspection PyTypeChecker
        signal.signal(signal.SIGTERM, shutdown)
        # noinspection PyTypeChecker
        signal.signal(signal.SIGINT, shutdown)

    interval = int(config.get("interval", "300"))  # interval in seconds; default are 5 Minutes
    log_level = config.get("loglevel", "INFO")  # set loglevel by Name
    debug_batch = config.get("debug_batch", args.debug_batch)  # set loglevel for batching (influx)

    if "netatmo" in config:
        client_id = config.get("netatmo").get("client_id", None)
        client_secret = config.get("netatmo").get("client_secret", None)
        refresh_token = config.get("netatmo").get("refresh_token", None)

    if "influx" in config:
        influx_host = config.get("influx").get("influx_host", "localhost")
        influx_port = config.get("influx").get("influx_port", "8086")
        influx_bucket = config.get("influx").get("influx_bucket", "netatmo")
        influx_protocol = config.get("influx").get("influx_protocol", "http")
        influx_token = config.get("influx").get("influx_token", None)
        influx_org = config.get("influx").get("influx_org", None)

    # Environment Variables takes precedence over config if set
    # global
    interval = int(getenv("INTERVAL", interval))
    log_level = getenv("LOGLEVEL", VerbosityLevel(args.verbosity).name if args.verbosity > 0 else log_level)
    debug_batch = getenv("DEBUG_BATCH", debug_batch)
    # netatmo
    client_id = getenv("NETATMO_CLIENT_ID", client_id)
    client_secret = getenv("NETATMO_CLIENT_SECRET", client_secret)
    # refresh_token needs to be persisted in the config, but can be set as env var for first run
    refresh_token = getenv("NETATMO_REFRESH_TOKEN", refresh_token)
    # influx
    influx_host = getenv("INFLUX_HOST", influx_host)
    influx_port = getenv("INFLUX_PORT", influx_port)
    influx_bucket = getenv("INFLUX_BUCKET", influx_bucket)
    influx_protocol = getenv("INFLUX_PROTOCOL", influx_protocol)
    influx_token = getenv("INFLUX_TOKEN", influx_token)
    influx_org = getenv("INFLUX_ORG", influx_org)

    # set logging level
    logger = logging.getLogger(__name__)
    log = configure_logging(logger, log_level)

    api = NetatmoAPI(
        client_id=client_id,
        client_secret=client_secret,
        refresh_token=refresh_token,
        token_file=args.token_file,
        log_level=log_level,
    )

    log.info("Netatmo Crawler ready...")
    while running:
        try:
            api.get_stations_data()
            stations = api.get_stations()

            with InfluxDBClient(
                url=f"{influx_protocol}://{influx_host}:{influx_port}",
                token=influx_token,
                org=influx_org,
                debug=True if debug_batch else False,
            ) as client:
                for _, _logger in client.conf.loggers.items():
                    _level = log_level if debug_batch else "NOTSET"
                    configure_logging(level=_level, logger=_logger)

                with client.write_api(
                    success_callback=influx_callback.success,
                    error_callback=influx_callback.error,
                    retry_callback=influx_callback.retry,
                ) as write_client:
                    for station_id, station in stations.items():
                        measurements = []

                        log.debug(f"Station Data: {station}")
                        home_name = station.get("home_name", "Unknown")
                        station_name = station.get("station_name", "Unknown")
                        station_module_name = station.get("module_name", "Unknown")
                        station_module_type = station.get("type", "Unknown")
                        station_place = station.get("place", {})
                        station_long_lat = station_place.get("location", [])

                        station_data = {
                            "altitude": station_place.get("altitude"),
                            "country": station_place.get("country", "Unknown"),
                            "city": station_place.get("city", "Unknown"),
                            "timezone": station_place.get("timezone", "Unknown"),
                            "longitude": safe_list_get(station_long_lat, 0),
                            "latitude": safe_list_get(station_long_lat, 1),
                        }

                        for key, value in station_data.items():
                            measurements.append(
                                {
                                    "measurement": key,
                                    "tags": {"home": home_name, "station": station_name, "type": station_module_type},
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
                            station_sensor_data, home_name, station_name, station_module_name, station_module_type
                        )

                        for module in station.get("modules", []):
                            log.debug(f"Module Data: {module}")
                            module_name = module.get("module_name")
                            module_type = module.get("type")

                            module_sensor_data = module.get("dashboard_data")

                            if module_sensor_data is None:
                                continue

                            for sensor in ["rf_status", "battery_vp", "battery_percent"]:
                                module_sensor_data.update({sensor: module.get(sensor)})

                            measurements += get_sensor_data(
                                module_sensor_data, home_name, station_name, module_name, module_type
                            )

                        # noinspection PyTypeChecker
                        write_client.write(
                            bucket=influx_bucket, org=influx_org, record=measurements, write_precision=WritePrecision.S
                        )
        except (json.decoder.JSONDecodeError, requests.exceptions.JSONDecodeError) as error:
            log.error(f"JSON Decode Error. Retry in {interval} second(s)...")
            log.debug(error)
        except NetatmoThrottlingError as error:
            log.error(f"API Throttling. Retry in {interval} second(s)...")
            log.debug(error)
        except NetatmoAPIError as error:
            log.error(f"API Error. Retry in {interval} second(s)...")
            log.debug(error)
        except NetatmoAuthError as error:
            log.error(f"Auth Error. Retry in {interval} second(s)...")
            log.debug(error)
        finally:
            sleep(interval)
