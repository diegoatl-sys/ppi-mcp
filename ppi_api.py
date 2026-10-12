"""Cliente mínimo (solo lectura) de la API pública de PPI - Portfolio Personal Inversiones.

Replica el login y los endpoints de market data del SDK oficial `ppi_client`,
pero sin sus dependencias (websockets, pytest, etc.), que no compilan en
versiones nuevas de Python. Solo expone consultas: no hay órdenes ni datos de cuenta.
"""

from __future__ import annotations

import asyncio
import os
from datetime import date
from typing import Any

import httpx

# Valores públicos que usa el SDK oficial de PPI para identificar al cliente.
_AUTHORIZED_CLIENT = "API_CLI_PYTHON"
_CLIENT_KEY_PROD = "pp19PythonApp12"
_CLIENT_KEY_SANDBOX = "ppPYTHONSb"

_URL_PROD = "https://clientapi.portfoliopersonal.com/api/1.0/"
_URL_SANDBOX = "https://clientapisbx.portfoliopersonal.com/api/1.0/"


class PPIError(Exception):
    """Error devuelto por la API de PPI, con un mensaje legible."""


class PPIClient:
    def __init__(self, api_key: str, api_secret: str, sandbox: bool = False, timeout: float = 20.0):
        if not api_key or not api_secret:
            raise PPIError("Faltan PPI_API_KEY y/o PPI_API_SECRET.")
        self._api_key = api_key
        self._api_secret = api_secret
        self._base_headers = {
            "AuthorizedClient": _AUTHORIZED_CLIENT,
            "ClientKey": _CLIENT_KEY_SANDBOX if sandbox else _CLIENT_KEY_PROD,
            "Accept": "application/json",
            "User-Agent": "ppi-mcp (solo lectura)",
        }
        self._http = httpx.AsyncClient(
            base_url=_URL_SANDBOX if sandbox else _URL_PROD, timeout=timeout
        )
        self._token: str | None = None
        self._lock = asyncio.Lock()

    @classmethod
    def desde_entorno(cls) -> "PPIClient":
        return cls(
            api_key=os.environ.get("PPI_API_KEY", "").strip(),
            api_secret=os.environ.get("PPI_API_SECRET", "").strip(),
            sandbox=os.environ.get("PPI_SANDBOX", "").strip().lower() in ("1", "true", "si", "sí"),
        )

    async def cerrar(self) -> None:
        await self._http.aclose()

    # ---------------------------------------------------------------- login
    async def _login(self) -> None:
        headers = {
            **self._base_headers,
            "ApiKey": self._api_key,
            "ApiSecret": self._api_secret,
            "Content-Type": "application/json",
        }
        r = await self._http.post("Account/LoginApi", headers=headers)
        if r.status_code != 200:
            raise PPIError(f"PPI rechazó el login (HTTP {r.status_code}): {_texto_error(r)}")
        self._token = r.json()["accessToken"]

    async def _token_vigente(self, forzar: bool = False) -> str:
        async with self._lock:
            if forzar or self._token is None:
                await self._login()
            assert self._token is not None
            return self._token

    async def _get(self, ruta: str, params: dict[str, Any] | None = None) -> Any:
        params = {k: v for k, v in (params or {}).items() if v is not None}
        for intento in range(2):
            token = await self._token_vigente(forzar=intento > 0)
            headers = {**self._base_headers, "Authorization": f"Bearer {token}"}
            r = await self._http.get(ruta, params=params, headers=headers)
            if r.status_code == 401 and intento == 0:
                continue  # token vencido: vuelve a loguear una vez
            if r.status_code != 200:
                raise PPIError(f"PPI respondió HTTP {r.status_code} en {ruta}: {_texto_error(r)}")
            return r.json() if r.text else None
        raise PPIError("PPI sigue respondiendo 401 después de volver a loguear.")

    # ----------------------------------------------------------- market data
    async def cotizacion(self, ticker: str, tipo: str, plazo: str) -> Any:
        return await self._get(
            "MarketData/Current", {"ticker": ticker, "type": tipo, "settlement": plazo}
        )

    async def puntas(self, ticker: str, tipo: str, plazo: str) -> Any:
        return await self._get(
            "MarketData/Book", {"ticker": ticker, "type": tipo, "settlement": plazo}
        )

    async def intradiario(self, ticker: str, tipo: str, plazo: str) -> Any:
        return await self._get(
            "MarketData/Intraday", {"ticker": ticker, "type": tipo, "settlement": plazo}
        )

    async def historico(
        self, ticker: str, tipo: str, plazo: str, desde: date, hasta: date, incluir_vencidos: bool = False
    ) -> Any:
        params = {
            "ticker": ticker,
            "type": tipo,
            "dateFrom": desde.isoformat(),
            "dateTo": hasta.isoformat(),
            "settlement": plazo,
        }
        if incluir_vencidos:
            params["skipFilter"] = "true"
        return await self._get("MarketData/Search", params)

    async def buscar_especie(self, ticker: str = "", nombre: str = "", mercado: str = "", tipo: str = "") -> Any:
        return await self._get(
            "MarketData/SearchInstrument",
            {"ticker": ticker, "name": nombre, "market": mercado, "type": tipo},
        )

    async def estimar_bono(
        self,
        ticker: str,
        fecha: date,
        precio: float,
        tipo_cantidad: str,
        cantidad: float | None = None,
        monto: float | None = None,
    ) -> Any:
        return await self._get(
            "MarketData/Bonds/Estimate",
            {
                "ticker": ticker,
                "date": fecha.isoformat(),
                "quantityType": tipo_cantidad,
                "quantity": cantidad,
                "amountOfMoney": monto,
                "price": precio,
            },
        )

    # --------------------------------------------------------- configuración
    async def tipos_de_instrumento(self) -> Any:
        return await self._get("Configuration/InstrumentTypes")

    async def plazos(self) -> Any:
        return await self._get("Configuration/Settlements")

    async def mercados(self) -> Any:
        return await self._get("Configuration/Markets")

    async def tipos_de_cantidad(self) -> Any:
        return await self._get("Configuration/QuantityTypes")


def _texto_error(r: httpx.Response) -> str:
    try:
        cuerpo = r.json()
    except ValueError:
        return (r.text or "(sin detalle)")[:500]
    if isinstance(cuerpo, dict):
        for clave in ("message", "Message", "error", "title", "detail"):
            if cuerpo.get(clave):
                return str(cuerpo[clave])[:500]
    return str(cuerpo)[:500]


def extraer_precio(dato: Any) -> float | None:
    """Saca el último precio de una respuesta de MarketData/Current."""
    if isinstance(dato, list) and dato:
        dato = dato[-1]
    if not isinstance(dato, dict):
        return None
    for clave in ("price", "Price", "lastPrice", "last", "close", "closingPrice"):
        valor = dato.get(clave)
        if isinstance(valor, (int, float)) and valor > 0:
            return float(valor)
    return None
