"""Read-only financial IOL client, with one in-memory token per session.

Only POST /token is permitted; the client exposes no trading or cancellation
methods. Error messages never include response bodies, tokens or credentials.
"""
from __future__ import annotations

from datetime import date, datetime
import time
import re

import requests

from .credentials import load_credentials

BASE_URL = "https://api.invertironline.com"


class IOLError(RuntimeError):
    pass


class IOLDataError(IOLError):
    pass


class IOLHTTPError(IOLError):
    def __init__(self, status, endpoint):
        self.status = status
        self.endpoint = endpoint
        super().__init__(f"IOL respondió HTTP {status} al consultar {endpoint}")


def _iso_date(value):
    if isinstance(value, datetime):
        value = value.date()
    if isinstance(value, date):
        return value.isoformat()
    try:
        return date.fromisoformat(str(value)[:10]).isoformat()
    except ValueError:
        raise IOLDataError("El rango de fechas no es válido") from None


class IOLClient:
    def __init__(self, config_path, username, credential_target="secret_key", *,
                 session=None, secret_loader=None, clock=None, sleep=None):
        if not isinstance(username, str) or not username.strip():
            raise IOLError("Configurar el usuario de IOL")
        self.config_path = config_path
        self.username = username.strip()
        self.credential_target = credential_target
        self.session = session or requests.Session()
        self._loader = secret_loader or load_credentials
        self._clock = clock or time.monotonic
        self._sleep = sleep or time.sleep
        self._access_token = None
        self._refresh_token = None
        self._expires = 0
        self.date_filter_supported = True

    def _token_request(self, data):
        try:
            response = self.session.post(BASE_URL + "/token", data=data,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=(10, 45), allow_redirects=False, verify=True)
            try:
                if response.status_code != 200:
                    raise IOLHTTPError(response.status_code, "autenticación")
                payload = response.json()
            finally:
                response.close()
        except requests.RequestException:
            raise IOLError("No se pudo completar la autenticación HTTPS con IOL") from None
        except ValueError:
            raise IOLDataError("IOL devolvió una autenticación con formato inválido") from None
        if not isinstance(payload, dict):
            raise IOLDataError("IOL devolvió una autenticación con formato inválido")
        token = payload.get("access_token")
        refresh = payload.get("refresh_token")
        token_type = payload.get("token_type", "bearer")
        if not isinstance(token, str) or not token.strip() or str(token_type).lower() != "bearer":
            raise IOLDataError("IOL no devolvió un token de acceso válido")
        try:
            lifetime = max(1, int(payload.get("expires_in", 900)))
        except (TypeError, ValueError):
            raise IOLDataError("IOL no devolvió un vencimiento de token válido") from None
        self._access_token = token
        self._refresh_token = refresh if isinstance(refresh, str) and refresh else None
        self._expires = self._clock() + lifetime
        return token

    def authenticate(self):
        if self._access_token and self._clock() < self._expires - 30:
            return self._access_token
        if self._refresh_token:
            return self._refresh()
        password = self._loader("IOL", self.config_path, self.credential_target)
        return self._token_request({"username": self.username, "password": password,
                                    "grant_type": "password"})

    def _refresh(self):
        if not self._refresh_token:
            raise IOLError("La sesión IOL venció; iniciar una nueva actualización")
        refresh = self._refresh_token
        self._access_token = self._refresh_token = None
        self._expires = 0
        return self._token_request({"refresh_token": refresh, "grant_type": "refresh_token"})

    def _get(self, endpoint, params=None):
        token = self.authenticate()
        refreshed = False
        attempts = 0
        while True:
            try:
                response = self.session.get(BASE_URL + endpoint,
                    params=params, headers={"Authorization": "Bearer " + token},
                    timeout=(10, 60), allow_redirects=False, verify=True)
            except requests.RequestException:
                if attempts < 2:
                    attempts += 1
                    self._sleep(attempts)
                    continue
                raise IOLError("No se pudo completar una consulta HTTPS a IOL") from None
            try:
                status = response.status_code
                if status == 401 and not refreshed and self._refresh_token:
                    response.close()
                    token = self._refresh()
                    refreshed = True
                    continue
                if (status == 429 or 500 <= status <= 599) and attempts < 2:
                    attempts += 1
                    try:
                        delay = min(10, max(1, int(response.headers.get("Retry-After", attempts))))
                    except (TypeError, ValueError):
                        delay = attempts
                    response.close()
                    self._sleep(delay)
                    continue
                if status != 200:
                    raise IOLHTTPError(status, endpoint)
                try:
                    return response.json()
                except ValueError:
                    raise IOLDataError("IOL devolvió datos con formato inválido") from None
            finally:
                response.close()

    @staticmethod
    def _collection(payload, keys, description):
        if isinstance(payload, list):
            return payload
        if isinstance(payload, dict):
            for key in keys:
                result = payload.get(key)
                if isinstance(result, list):
                    total = payload.get("total", payload.get("totalCount"))
                    if total is not None:
                        try:
                            if int(total) > len(result):
                                raise IOLDataError(f"La consulta {description} está paginada o incompleta")
                        except (TypeError, ValueError):
                            raise IOLDataError(f"La consulta {description} informó un total inválido") from None
                    return result
        raise IOLDataError(f"IOL devolvió una colección inválida para {description}")

    def get_operations(self, start, end):
        params = {"filtro.estado": "terminadas", "filtro.pais": "argentina"}
        if self.date_filter_supported:
            params.update({"filtro.fechaDesde": _iso_date(start),
                           "filtro.fechaHasta": _iso_date(end)})
        try:
            payload = self._get("/api/v2/operaciones", params)
        except IOLHTTPError as exc:
            if exc.status not in (400, 422) or not self.date_filter_supported:
                raise
            # Some API account versions do not expose a server-side date filter.
            # Never delete local history when falling back to the available list.
            self.date_filter_supported = False
            payload = self._get("/api/v2/operaciones",
                {"filtro.estado": "terminadas", "filtro.pais": "argentina"})
        return self._collection(payload, ("operaciones", "items", "data", "results"), "operaciones")

    def get_account(self):
        payload = self._get("/api/v2/estadocuenta")
        if not isinstance(payload, dict) or not isinstance(payload.get("cuentas"), list):
            raise IOLDataError("IOL devolvió un estado de cuenta inválido")
        return payload

    def get_portfolio(self):
        payload = self._get("/api/v2/portafolio/argentina")
        if isinstance(payload, list):
            return {"activos": payload}
        if not isinstance(payload, dict) or not isinstance(payload.get("activos"), list):
            raise IOLDataError("IOL devolvió una cartera inválida")
        return payload

    def get_historical(self, symbol, start, end):
        if not re.fullmatch(r"[A-Za-z0-9.]{1,24}", str(symbol)):
            raise IOLDataError("El símbolo de cotización no es válido")
        endpoint = (f"/api/v2/bCBA/Titulos/{symbol}/Cotizacion/seriehistorica/"
                    f"{_iso_date(start)}/{_iso_date(end)}/sinAjustar")
        return self._collection(self._get(endpoint),
            ("cotizaciones", "items", "data", "results"), "cotizaciones históricas")

    def close(self):
        self._access_token = self._refresh_token = None
        self._expires = 0
        self.session.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
