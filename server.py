"""Conector MCP (solo lectura) de precios de PPI para usar desde Claude.

Expone cotizaciones, puntas, históricos, búsqueda de especies, dólar MEP/CCL
implícito y la calculadora de bonos de PPI. No opera ni lee datos de cuenta.

Variables de entorno:
  PPI_API_KEY, PPI_API_SECRET   claves de la API de PPI
  MCP_TOKEN                     clave larga y aleatoria; forma parte de la URL del conector
  PORT                          puerto (Render lo define solo; por defecto 8000)
  PPI_SANDBOX                   "true" para usar el entorno de pruebas de PPI (opcional)
"""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import date
from typing import Any, Awaitable, TypeVar

import httpx
import uvicorn
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations
from pydantic import BaseModel, Field
from starlette.requests import Request
from starlette.responses import PlainTextResponse

from ppi_api import PPIClient, PPIError, extraer_precio

PLAZO_DEFAULT = "A-24HS"
MAX_ESPECIES = 30
MAX_FILAS_HISTORICO = 400
MAX_RESULTADOS_BUSQUEDA = 25

INSTRUCCIONES = f"""\
Conector de solo lectura a la API de PPI (Portfolio Personal Inversiones, Argentina).
Sirve para verificar precios de acciones, bonos, ONs, letras y CEDEARs del mercado argentino.

- Cada especie se identifica con ticker + tipo de instrumento + plazo de liquidación.
- Tipos válidos: ACCIONES, BONOS, LETRAS, ON, CEDEARS, ETF, ACCIONES-USA, OPCIONES,
  FUTUROS, CAUCIONES, FCI, FCI-EXTERIOR, LICITACIONES, NOBAC, LEBAC.
- Plazos válidos: INMEDIATA, A-24HS, A-48HS, A-72HS. Por defecto: {PLAZO_DEFAULT}.
- Bonos y ONs en dólares tienen tres especies: AL30 (pesos), AL30D (dólar MEP), AL30C (dólar cable).
  Los bonos cotizan cada 100 de valor nominal.
- Si no sabés el tipo o el ticker exacto, usá `buscar_especie`.
- Para varias especies a la vez usá `cotizaciones` en lugar de llamar `cotizacion` muchas veces.
- `calcular_bono`: el precio va en la moneda de emisión. Para bonos u ONs en dólares,
  pasá el precio en USD cada 100 VN (el de la especie D o C), nunca el precio en pesos.
- Fuera del horario de mercado y en fines de semana, `puntas` puede venir vacío o con error;
  los precios son los del último cierre.
"""

T = TypeVar("T")

mcp = MCPServer(name="ppi-precios", title="PPI - Precios de mercado", instructions=INSTRUCCIONES)
SOLO_LECTURA = ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=True)

_ppi: PPIClient | None = None
_cache_config: dict[str, Any] | None = None


def ppi() -> PPIClient:
    global _ppi
    if _ppi is None:
        _ppi = PPIClient.desde_entorno()
    return _ppi


async def _llamar(coro: Awaitable[T]) -> T:
    """Convierte errores de PPI o de red en mensajes que Claude puede leer."""
    try:
        return await coro
    except PPIError as e:
        raise ToolError(
            f"{str(e).rstrip('.')}. Si el problema es el tipo o el plazo, consultá `valores_validos`."
        ) from e
    except httpx.HTTPError as e:
        raise ToolError(f"No se pudo conectar con PPI: {e.__class__.__name__}: {e}") from e


def _norm(texto: str) -> str:
    return texto.strip().upper()


# --------------------------------------------------------------------- tools
@mcp.tool(annotations=SOLO_LECTURA)
async def cotizacion(ticker: str, tipo: str = "ACCIONES", plazo: str = PLAZO_DEFAULT) -> dict[str, Any]:
    """Último precio y datos del día de una especie en PPI.

    Args:
        ticker: Ticker de BYMA, p. ej. GGAL, YPFD, AL30, AL30D, GD30, AAPL (CEDEAR).
        tipo: Tipo de instrumento de PPI (p. ej. ACCIONES, BONOS, CEDEARS, ON, LETRAS).
        plazo: Plazo de liquidación (p. ej. A-24HS o INMEDIATA).
    """
    t, ti, pl = _norm(ticker), _norm(tipo), _norm(plazo)
    datos = await _llamar(ppi().cotizacion(t, ti, pl))
    return {"ticker": t, "tipo": ti, "plazo": pl, "datos": datos}


class Especie(BaseModel):
    ticker: str = Field(description="Ticker, p. ej. GGAL o AL30D")
    tipo: str = Field(default="ACCIONES", description="Tipo de instrumento de PPI")
    plazo: str = Field(default=PLAZO_DEFAULT, description="Plazo de liquidación")


@mcp.tool(annotations=SOLO_LECTURA)
async def cotizaciones(especies: list[Especie]) -> dict[str, Any]:
    """Cotiza varias especies de una vez (hasta 30). Ideal para controlar una cartera o una lista de operaciones.

    Devuelve el último precio de cada una; las que fallan traen el error en vez de cortar todo el pedido.
    """
    if not especies:
        raise ToolError("Pasá al menos una especie.")
    if len(especies) > MAX_ESPECIES:
        raise ToolError(f"Máximo {MAX_ESPECIES} especies por llamada; dividí el pedido.")

    sem = asyncio.Semaphore(5)

    async def una(e: Especie) -> dict[str, Any]:
        t, ti, pl = _norm(e.ticker), _norm(e.tipo), _norm(e.plazo)
        async with sem:
            try:
                datos = await ppi().cotizacion(t, ti, pl)
                return {"ticker": t, "tipo": ti, "plazo": pl, "precio": extraer_precio(datos), "datos": datos}
            except (PPIError, httpx.HTTPError) as err:
                return {"ticker": t, "tipo": ti, "plazo": pl, "error": str(err)}

    resultados = await asyncio.gather(*(una(e) for e in especies))
    return {"resultados": resultados}


@mcp.tool(annotations=SOLO_LECTURA)
async def puntas(ticker: str, tipo: str = "ACCIONES", plazo: str = PLAZO_DEFAULT) -> dict[str, Any]:
    """Libro de órdenes (puntas compradoras y vendedoras) de una especie en PPI.

    Solo tiene datos en horario de mercado; fuera de horario puede venir vacío o con error.
    """
    t, ti, pl = _norm(ticker), _norm(tipo), _norm(plazo)
    datos = await _llamar(ppi().puntas(t, ti, pl))
    return {"ticker": t, "tipo": ti, "plazo": pl, "datos": datos}


@mcp.tool(annotations=SOLO_LECTURA)
async def historico(
    ticker: str,
    desde: date,
    hasta: date | None = None,
    tipo: str = "ACCIONES",
    plazo: str = PLAZO_DEFAULT,
    incluir_vencidos: bool = False,
) -> dict[str, Any]:
    """Serie histórica de precios diarios de una especie en PPI.

    Args:
        ticker: Ticker de la especie.
        desde: Fecha inicial (AAAA-MM-DD).
        hasta: Fecha final (AAAA-MM-DD). Por defecto, hoy.
        tipo: Tipo de instrumento de PPI.
        plazo: Plazo de liquidación.
        incluir_vencidos: True para especies vencidas o que ya no cotizan.
    """
    hasta = hasta or date.today()
    if desde > hasta:
        raise ToolError("La fecha 'desde' es posterior a 'hasta'.")
    t, ti, pl = _norm(ticker), _norm(tipo), _norm(plazo)
    filas = await _llamar(ppi().historico(t, ti, pl, desde, hasta, incluir_vencidos))
    recortado = isinstance(filas, list) and len(filas) > MAX_FILAS_HISTORICO
    if recortado:
        filas = filas[-MAX_FILAS_HISTORICO:]
    return {
        "ticker": t,
        "tipo": ti,
        "plazo": pl,
        "desde": desde.isoformat(),
        "hasta": hasta.isoformat(),
        "aviso": f"Se devuelven solo las últimas {MAX_FILAS_HISTORICO} filas; acortá el rango." if recortado else None,
        "filas": filas,
    }


@mcp.tool(annotations=SOLO_LECTURA)
async def buscar_especie(ticker: str = "", nombre: str = "", tipo: str = "", mercado: str = "") -> dict[str, Any]:
    """Busca especies en PPI por ticker o por nombre, para confirmar el ticker y el tipo exactos.

    Args:
        ticker: Ticker completo o parcial.
        nombre: Parte del nombre (p. ej. "Galicia", "Pampa").
        tipo: Filtro opcional por tipo de instrumento.
        mercado: Filtro opcional por mercado (ver `valores_validos`).
    """
    if not ticker.strip() and not nombre.strip():
        raise ToolError("Indicá un ticker o un nombre para buscar.")
    datos = await _llamar(
        ppi().buscar_especie(_norm(ticker), nombre.strip(), mercado.strip(), _norm(tipo) if tipo else "")
    )
    total = len(datos) if isinstance(datos, list) else None
    recortado = total is not None and total > MAX_RESULTADOS_BUSQUEDA
    return {
        "total": total,
        "aviso": (
            f"Hay {total} resultados; se muestran los primeros {MAX_RESULTADOS_BUSQUEDA}. "
            "Afiná la búsqueda con ticker o tipo."
        )
        if recortado
        else None,
        "resultados": datos[:MAX_RESULTADOS_BUSQUEDA] if recortado else datos,
    }


@mcp.tool(annotations=SOLO_LECTURA)
async def dolar_mep_ccl(bono: str = "AL30", plazo: str = PLAZO_DEFAULT) -> dict[str, Any]:
    """Dólar MEP y contado con liquidación implícitos en un bono, con el último precio operado en PPI.

    MEP = precio en pesos / precio en dólar MEP (especie terminada en D).
    CCL = precio en pesos / precio en dólar cable (especie terminada en C).

    Args:
        bono: Ticker base del bono en pesos, p. ej. AL30 o GD30.
        plazo: Plazo de liquidación de las tres especies.
    """
    base, pl = _norm(bono), _norm(plazo)
    tickers = {"pesos": base, "mep": f"{base}D", "cable": f"{base}C"}
    respuestas = await asyncio.gather(
        *(ppi().cotizacion(t, "BONOS", pl) for t in tickers.values()), return_exceptions=True
    )
    precios: dict[str, Any] = {}
    errores: dict[str, str] = {}
    for (clave, t), resp in zip(tickers.items(), respuestas):
        if isinstance(resp, BaseException):
            errores[t] = str(resp)
            continue
        p = extraer_precio(resp)
        if p is None:
            errores[t] = f"No se encontró un precio en la respuesta: {resp}"
        precios[clave] = {"ticker": t, "precio": p, "datos": resp}

    if "pesos" not in precios or precios["pesos"]["precio"] is None:
        raise ToolError(f"No se pudo obtener el precio en pesos de {base}. Errores: {errores}")

    ars = precios["pesos"]["precio"]

    def tc(clave: str) -> float | None:
        p = precios.get(clave, {}).get("precio")
        return round(ars / p, 2) if p else None

    return {
        "bono": base,
        "plazo": pl,
        "dolar_mep": tc("mep"),
        "dolar_ccl": tc("cable"),
        "precios": {k: v["precio"] for k, v in precios.items()},
        "detalle": precios,
        "errores": errores or None,
        "nota": "Calculado con el último precio operado de cada especie, no con puntas.",
    }


@mcp.tool(annotations=SOLO_LECTURA)
async def calcular_bono(
    ticker: str,
    precio: float,
    cantidad: float = 100,
    fecha: date | None = None,
    tipo_cantidad: str = "PAPELES",
) -> dict[str, Any]:
    """Calculadora de bonos de PPI: TIR, duration, paridad, flujo de fondos, etc. a un precio dado.

    IMPORTANTE: el precio va en la moneda de emisión del bono, cada 100 VN.
    Para bonos u ONs en dólares (AL30, GD30, ONs en USD) usá el precio en dólares
    de la especie D (MEP) o C (cable), p. ej. el de AL30D. NUNCA el precio en pesos:
    da una TIR sin sentido.

    Args:
        ticker: Ticker base del bono (p. ej. AL30, GD30, o el de una ON).
        precio: Precio cada 100 VN en la moneda de emisión (ver arriba).
        cantidad: Cantidad de títulos (valor nominal) a evaluar.
        fecha: Fecha de liquidación (AAAA-MM-DD). Por defecto, hoy.
        tipo_cantidad: PAPELES (por defecto), DINERO o CANTIDAD-TOTAL.
    """
    fecha = fecha or date.today()
    datos = await _llamar(
        ppi().estimar_bono(_norm(ticker), fecha, precio, _norm(tipo_cantidad), cantidad=cantidad)
    )

    resumen: dict[str, Any] | None = None
    aviso: str | None = None
    if isinstance(datos, dict):
        tir, md, paridad = datos.get("tir"), datos.get("md"), datos.get("parity")
        resumen = {
            "tir_pct": round(tir * 100, 2) if isinstance(tir, (int, float)) else None,
            "duration_modificada": round(md, 2) if isinstance(md, (int, float)) else None,
            "paridad_pct": round(paridad * 100, 2) if isinstance(paridad, (int, float)) else None,
            "moneda": datos.get("currency"),
            "titulo": datos.get("title"),
        }
        if isinstance(tir, (int, float)) and not -0.5 < tir < 1.5:
            aviso = (
                f"La TIR da {tir * 100:.1f}%, un valor sin sentido. Casi seguro el precio está en otra "
                f"moneda o base: este bono se calcula en {datos.get('currency') or 'su moneda de emisión'}. "
                "Para bonos en dólares usá el precio en USD cada 100 VN (especie D o C), no el precio en pesos."
            )

    return {
        "ticker": _norm(ticker),
        "precio": precio,
        "fecha": fecha.isoformat(),
        "aviso": aviso,
        "resumen": resumen,
        "datos": datos,
    }


@mcp.tool(annotations=SOLO_LECTURA)
async def valores_validos() -> dict[str, Any]:
    """Lista los tipos de instrumento, plazos de liquidación, mercados y tipos de cantidad que acepta PPI."""
    global _cache_config
    if _cache_config is None:
        tipos, plazos, mercados, cantidades = await _llamar(
            asyncio.gather(
                ppi().tipos_de_instrumento(),
                ppi().plazos(),
                ppi().mercados(),
                ppi().tipos_de_cantidad(),
            )
        )
        _cache_config = {
            "tipos_de_instrumento": tipos,
            "plazos": plazos,
            "mercados": mercados,
            "tipos_de_cantidad": cantidades,
        }
    return _cache_config


# ---------------------------------------------------------------- arranque
@mcp.custom_route("/", methods=["GET"], include_in_schema=False)
async def salud(_: Request) -> PlainTextResponse:
    return PlainTextResponse("ok")


def crear_app():
    token = os.environ.get("MCP_TOKEN", "").strip()
    if len(token) < 24 or not token.replace("-", "").replace("_", "").isalnum():
        sys.exit(
            "MCP_TOKEN falta o es muy corto. Usá al menos 24 caracteres alfanuméricos "
            "(guiones permitidos); forma parte de la URL secreta del conector."
        )
    return mcp.streamable_http_app(
        streamable_http_path=f"/mcp/{token}",
        stateless_http=True,
        json_response=True,
        host="0.0.0.0",
    )


if __name__ == "__main__":
    if not os.environ.get("PPI_API_KEY") or not os.environ.get("PPI_API_SECRET"):
        sys.exit("Faltan PPI_API_KEY y/o PPI_API_SECRET.")
    # access_log=False: así la URL secreta no queda escrita en los logs del hosting.
    uvicorn.run(crear_app(), host="0.0.0.0", port=int(os.environ.get("PORT", "8000")), access_log=False)
