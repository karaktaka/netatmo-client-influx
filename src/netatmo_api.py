#!/usr/bin/env python3
# encoding=utf-8
"""
Implements OAuth2 token refresh and weather station data fetching.
"""

import json
import logging
from typing import Any, Dict, Optional, Tuple

import requests
from requests import Response

LOG = logging.getLogger(__name__)

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
    ):
        """
        Initialize Netatmo authentication.

        Args:
            client_id: OAuth2 client ID from Netatmo developer console
            client_secret: OAuth2 client secret from Netatmo developer console
            refresh_token: Initial refresh token (persisted in config)
        """
        self.client_id = client_id
        self.client_secret = client_secret
        self.token_file = token_file
        self.refresh_token = refresh_token
        self.access_token: Optional[str] = None
        self.token_expires_in: Optional[int] = None

        try:
            with open(self.token_file, "r+") as _f:
                _token_data = json.load(_f)
                self.access_token = _token_data.get("access_token")
                self.refresh_token = _token_data.get("refresh_token")
                self.token_expires_in = _token_data.get("expiration")
        except (FileNotFoundError, json.JSONDecodeError):
            LOG.info("No existing token file found, will create a new one upon refresh.")

    def refresh(self) -> Tuple[str, str, Optional[int]]:
        """
        Refresh the OAuth2 access token using the refresh token.

        Returns:
            Tuple of (access_token, new_refresh_token, token_expires_in)

        Raises:
            NetatmoAuthError: If token refresh fails
        """
        try:
            LOG.debug("Refreshing Netatmo access token...")

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
            self.token_expires_in = token_data.get("expires_in")
            new_refresh_token = token_data.get("refresh_token", self.refresh_token)

            if new_refresh_token != self.refresh_token:
                LOG.info("Refresh token updated")
                self.refresh_token = new_refresh_token

            _token = {
                "access_token": self.access_token,
                "refresh_token": self.refresh_token,
                "expiration": self.token_expires_in,
            }
            with open(self.token_file, "w") as _f:
                _f.write(json.dumps(_token, indent=2))

            return self.access_token, self.refresh_token, self.token_expires_in

        except requests.exceptions.RequestException as e:
            raise NetatmoAuthError(f"Failed to refresh token: {e}") from e

    @property
    def headers(self) -> Dict[str, str]:
        """Get authorization headers for API requests."""
        if not self.access_token or (self.token_expires_in and self.token_expires_in <= 60):
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

    def __init__(self, auth: NetatmoAuth):
        """
        Initialize the Weather Station API client.

        Args:
            auth: NetatmoAuth instance for authentication
        """
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
            LOG.debug("Fetching stations data from Netatmo API...")

            response = requests.post(
                NETATMO_STATIONS_DATA_ENDPOINT,
                headers=self.auth.headers,
                timeout=10,
            )

            self._handle_response_errors(response)

            data = response.json()
            self.stations_data = data.get("body", {}).get("devices", [])

            LOG.debug(f"Received data for {len(self.stations_data)} station(s)")
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
