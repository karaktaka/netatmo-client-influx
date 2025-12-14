#!/usr/bin/env python3
# encoding=utf-8
"""
Implements OAuth2 token refresh and weather station data fetching.
"""

import json
import logging
import time
from typing import Any, Dict, Optional, Tuple

import requests
from requests import Response

from helpers import configure_logging

# Netatmo API endpoints
NETATMO_AUTH_URL = "https://api.netatmo.com/oauth2/authorize"
NETATMO_TOKEN_URL = "https://api.netatmo.com/oauth2/token"
NETATMO_API_BASE_URL = "https://api.netatmo.com/api"
NETATMO_STATIONS_DATA_ENDPOINT = f"{NETATMO_API_BASE_URL}/getstationsdata"


class NetatmoAuthError(Exception):
    """Base exception for Netatmo authentication errors."""

    pass


class NetatmoAuthErrorTokenExpired(Exception):
    """Exception raised when Netatmo refresh token is expired."""

    pass


class NetatmoAPIError(Exception):
    """Base exception for Netatmo API errors."""

    pass


class NetatmoThrottlingError(NetatmoAPIError):
    """Exception raised when API throttling occurs."""

    pass


class NetatmoAuth:
    """
    Handles OAuth2 authentication and token management for Netatmo API.
    """

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        refresh_token: str,
        token_file: str,
        log: logging.Logger,
    ):
        """
        Initialize Netatmo authentication.

        Args:
            client_id: OAuth2 client ID from Netatmo developer console
            client_secret: OAuth2 client secret from Netatmo developer console
            refresh_token: Initial refresh token (persisted in config)
        """
        self.log = log
        self.client_id = client_id
        self.client_secret = client_secret
        self.token_file = token_file
        self.refresh_token = refresh_token
        self.access_token: Optional[str] = None
        self.token_expires_at: Optional[float] = None

        try:
            with open(self.token_file, "r+") as _f:
                _token_data = json.load(_f)
                self.access_token = _token_data.get("access_token")
                self.refresh_token = _token_data.get("refresh_token")
                self.token_expires_at = _token_data.get("expires_at")
        except (FileNotFoundError, json.JSONDecodeError):
            self.log.info("No existing token file found, will create a new one upon refresh.")

    def refresh(self) -> Tuple[str, str, Optional[int]]:
        """
        Refresh the OAuth2 access token using the refresh token.

        Returns:
            Tuple of (access_token, new_refresh_token, token_expires_in)

        Raises:
            NetatmoAuthError: If token refresh fails
        """
        try:
            self.log.debug("Refreshing Netatmo access token...")

            payload = {
                "grant_type": "refresh_token",
                "refresh_token": self.refresh_token,
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            }

            response = requests.post(
                NETATMO_TOKEN_URL,
                data=payload,
                timeout=10,
            )

            if response.status_code == 400:
                # Invalid grant error - refresh token expired
                try:
                    error_data = response.json()
                    if error_data.get("error") == "invalid_grant":
                        raise NetatmoAuthErrorTokenExpired(
                            "Refresh token expired! Please generate a new one in the Developer Console."
                        )
                except ValueError:
                    pass
                raise NetatmoAuthError(f"Failed to refresh token: {response.text}")

            response.raise_for_status()

            token_data = response.json()
            self.access_token = token_data.get("access_token")
            expires_in = token_data.get("expires_in")
            # Calculate absolute expiration timestamp
            self.token_expires_at = time.time() + expires_in if expires_in else None
            new_refresh_token = token_data.get("refresh_token", self.refresh_token)

            if new_refresh_token != self.refresh_token:
                self.log.info("Refresh token updated")
                self.refresh_token = new_refresh_token

            _token = {
                "access_token": self.access_token,
                "refresh_token": self.refresh_token,
                "expiration": self.token_expires_at,
            }
            with open(self.token_file, "w") as _f:
                _f.write(json.dumps(_token, indent=2))

            return self.access_token, self.refresh_token, self.token_expires_at

        except requests.exceptions.RequestException as e:
            raise NetatmoAuthError(f"Failed to refresh token: {e}") from e

    @property
    def headers(self) -> Dict[str, str]:
        """Get authorization headers for API requests."""
        # Check if token is expired or expires within 60 seconds
        time_until_expiration = float("inf")
        if self.token_expires_at is not None:
            time_until_expiration = self.token_expires_at - time.time()
            self.log.debug(f"Time until token expiration: {time_until_expiration} seconds")

        if not self.access_token or time_until_expiration <= 60:
            self.refresh()

        return {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json",
            "accept": "application/json",
            "User-Agent": "python/src/1.0",
        }


class NetatmoWeatherStationAPI:
    """
    Handles data fetching from Netatmo Weather Station API.
    """

    def __init__(self, auth: NetatmoAuth, log: logging.Logger):
        """
        Initialize the Weather Station API client.

        Args:
            auth: NetatmoAuth instance for authentication
        """
        self.log = log
        self.auth = auth
        self.stations_data: Dict[str, Any] = {}
        self.stations_status: Dict[str, Any] = {}

    def get_stations_data(self) -> Dict[str, Any]:
        """
        Fetch station topology data.

        Returns:
            Dictionary containing stations and their modules

        Raises:
            NetatmoAPIError: If API request fails
        """
        try:
            self.log.debug("Fetching stations data from Netatmo API...")

            response = requests.post(
                NETATMO_STATIONS_DATA_ENDPOINT,
                headers=self.auth.headers,
                timeout=10,
            )

            self._handle_response_errors(response)

            data = response.json()
            self.stations_data = data.get("body", {}).get("devices", [])

            self.log.debug(f"Received data for {len(self.stations_data)} station(s)")
            return data

        except requests.exceptions.RequestException as e:
            raise NetatmoAPIError(f"Failed to fetch stations data: {e}") from e

    def get_stations(self) -> Dict[str, Dict[str, Any]]:
        """
        Parse station data and return weather stations.

        Returns:
            Dictionary mapping station IDs to station data
        """
        stations: Dict[str, Dict[str, Any]] = {}

        for station in self.stations_data:
            station_dict: Dict[str, Any] = station if isinstance(station, dict) else {}
            station_id = station_dict.get("_id")
            if station_id:
                stations[station_id] = {
                    "id": station_id,
                    "station_name": station_dict.get("station_name", "Unknown"),
                    "date_setup": station_dict.get("date_setup"),
                    "last_setup": station_dict.get("last_setup"),
                    "type": station_dict.get("type"),
                    "last_status_store": station_dict.get("last_status_store"),
                    "module_name": station_dict.get("module_name", "Unknown"),
                    "firmware": station_dict.get("firmware"),
                    "wifi_status": station_dict.get("wifi_status"),
                    "reachable": station_dict.get("reachable", False),
                    "co2_calibrating": station_dict.get("co2_calibrating"),
                    "data_type": station_dict.get("data_type", []),
                    "place": station_dict.get("place", {}),
                    "home_id": station_dict.get("home_id"),
                    "home_name": station_dict.get("home_name", "Unknown"),
                    "dashboard_data": station_dict.get("dashboard_data", None),
                    "modules": [],
                }
            if not station_dict.get("modules"):
                continue

            for module in station_dict.get("modules", []):
                module_dict: Dict[str, Any] = module if isinstance(module, dict) else {}
                if module_dict.get("type") in ["NAMain", "NAWifiStation"]:
                    continue

                stations[station_id]["modules"].append(
                    {
                        "id": module_dict.get("_id"),
                        "type": module_dict.get("type"),
                        "module_name": module_dict.get("module_name", "Unknown"),
                        "last_setup": module_dict.get("last_setup"),
                        "data_type": module_dict.get("data_type", []),
                        "battery_percent": module_dict.get("battery_percent"),
                        "reachable": module_dict.get("reachable", False),
                        "firmware": module_dict.get("firmware"),
                        "last_message": module_dict.get("last_message"),
                        "last_seen": module_dict.get("last_seen"),
                        "rf_status": module_dict.get("rf_status"),
                        "battery_vp": module_dict.get("battery_vp"),
                        "dashboard_data": module_dict.get("dashboard_data", None),
                    }
                )

        return stations

    @staticmethod
    def _handle_response_errors(response: Response) -> None:
        """
        Handle API response errors.

        Args:
            response: requests Response object

        Raises:
            NetatmoThrottlingError: If API throttling occurs
            NetatmoAPIError: For other API errors
        """
        if response.status_code == 429:
            raise NetatmoThrottlingError("API throttling: too many requests")

        if response.status_code == 403:
            try:
                error_data = response.json()
                error_code = error_data.get("error", {}).get("code")
                if error_code == 1:  # Invalid access token
                    raise NetatmoAuthError("Invalid access token")
            except ValueError:
                pass
            raise NetatmoAPIError("Access forbidden (403)")

        if response.status_code >= 400:
            raise NetatmoAPIError(f"API request failed with status {response.status_code}: {response.text}")

        # Check for error in response body
        try:
            data = response.json()
            if not data.get("body"):
                error = data.get("error", {})
                if error:
                    raise NetatmoAPIError(f"API error: {error}")
        except ValueError:
            pass


class NetatmoAPI(NetatmoWeatherStationAPI):
    """
    Combined Netatmo API client for authentication and data fetching.
    """

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        refresh_token: str,
        token_file: str,
        log_level: str = "INFO",
    ):
        """
        Initialize the combined Netatmo API client.

        Args:
            client_id: OAuth2 client ID from Netatmo developer console
            client_secret: OAuth2 client secret from Netatmo developer console
            refresh_token: Initial refresh token (persisted in config)
            token_file: Path to the file where tokens are stored
        """
        logger = logging.getLogger(__name__)
        self.log = configure_logging(logger, log_level)

        auth = NetatmoAuth(
            client_id=client_id,
            client_secret=client_secret,
            refresh_token=refresh_token,
            token_file=token_file,
            log=self.log,
        )
        super().__init__(auth=auth, log=self.log)
