"""Minimal OpenSky Network client: session reuse, optional OAuth2, and
rate-limit-aware error reporting."""

import logging
import time
from typing import Any, List, Optional

import requests

import config

logger = logging.getLogger(__name__)

# Refresh the token this many seconds before OpenSky says it expires.
_TOKEN_REFRESH_MARGIN_SECONDS = 60.0


class OpenSkyError(Exception):
    """A fetch failed for a reason worth retrying with backoff."""


class RateLimitedError(OpenSkyError):
    """OpenSky returned 429; retry_after is the server hint in seconds."""

    def __init__(self, retry_after: Optional[float]):
        hint = f'{retry_after:.0f}s' if retry_after else 'unspecified'
        super().__init__(f'rate limited by OpenSky (retry after: {hint})')
        self.retry_after = retry_after


class OpenSkyClient:
    """Fetches state vectors for the configured bounding box."""

    def __init__(self) -> None:
        self._session = requests.Session()
        self._token: Optional[str] = None
        self._token_expires_at = 0.0

    @property
    def authenticated(self) -> bool:
        return bool(config.OPENSKY_CLIENT_ID and config.OPENSKY_CLIENT_SECRET)

    def _refresh_token(self) -> None:
        response = self._session.post(
            config.OPENSKY_TOKEN_URL,
            data={
                'grant_type': 'client_credentials',
                'client_id': config.OPENSKY_CLIENT_ID,
                'client_secret': config.OPENSKY_CLIENT_SECRET,
            },
            timeout=15)
        response.raise_for_status()
        payload = response.json()
        self._token = payload['access_token']
        expires_in = float(payload.get('expires_in', 1800))
        self._token_expires_at = (
            time.monotonic() + expires_in - _TOKEN_REFRESH_MARGIN_SECONDS)
        logger.info('Obtained OpenSky OAuth2 token (expires in %.0fs)',
                    expires_in)

    def _auth_headers(self) -> dict:
        if not self.authenticated:
            return {}
        if self._token is None or time.monotonic() >= self._token_expires_at:
            self._refresh_token()
        return {'Authorization': f'Bearer {self._token}'}

    def fetch_states(self) -> List[List[Any]]:
        """Return the raw state vectors, or raise an OpenSkyError subclass."""
        params = {
            'lamin': config.BBOX_LAMIN,
            'lomin': config.BBOX_LOMIN,
            'lamax': config.BBOX_LAMAX,
            'lomax': config.BBOX_LOMAX,
        }
        try:
            headers = self._auth_headers()
        except (requests.RequestException, KeyError, ValueError) as exc:
            raise OpenSkyError(f'token request failed: {exc}') from exc

        try:
            response = self._session.get(
                config.OPENSKY_API_URL, params=params, headers=headers,
                timeout=30)
        except requests.RequestException as exc:
            raise OpenSkyError(str(exc)) from exc

        if response.status_code == 429:
            retry_after = response.headers.get('Retry-After')
            try:
                parsed = float(retry_after) if retry_after else None
            except ValueError:
                parsed = None
            raise RateLimitedError(parsed)

        if response.status_code == 401 and self.authenticated:
            # Token invalidated server-side; force a refresh on the next call.
            self._token = None
            raise OpenSkyError('unauthorized; token will be refreshed')

        try:
            response.raise_for_status()
            data = response.json()
        except (requests.HTTPError, ValueError) as exc:
            raise OpenSkyError(str(exc)) from exc

        return data.get('states') or []
