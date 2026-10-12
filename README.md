# Conector PPI para Claude (precios, solo lectura)

Servidor MCP que le da a Claude acceso a los precios de PPI: acciones, bonos, ONs, letras y CEDEARs. **No opera ni lee datos de tu cuenta**: solo consulta market data.

| Herramienta | Qué hace |
|---|---|
| `cotizacion` | Último precio y datos del día de una especie |
| `cotizaciones` | Hasta 30 especies de una vez (para controlar una cartera u operaciones) |
| `puntas` | Libro de órdenes (compra / venta) |
| `historico` | Precios diarios entre dos fechas |
| `buscar_especie` | Busca por ticker o nombre para confirmar ticker y tipo |
| `dolar_mep_ccl` | MEP y CCL implícitos en un bono (AL30 / AL30D / AL30C por defecto) |
| `calcular_bono` | Calculadora de PPI: TIR, duration, flujo, etc. a un precio dado |
| `valores_validos` | Tipos de instrumento, plazos y mercados que acepta PPI |

## Archivos

- `server.py`: el servidor MCP.
- `ppi_api.py`: cliente de la API de PPI (login y market data).
- `probar_ppi.py`: prueba tus claves antes de publicar.
- `requirements.txt`, `.python-version`: dependencias y versión de Python para el hosting.
- `.env.example`: modelo de configuración. El `.env` real nunca se sube a ningún lado.

---

## Paso 1: Claves de API de PPI

Necesitás una **API Key** y un **API Secret** de PPI. Se generan desde tu cuenta en la web de PPI, en la sección de API. Si no la encontrás, pedíselas a tu asesor o escribí a api@portfoliopersonal.com.

Si podés elegir permisos, alcanza con consulta de mercado. Este servidor no usa ninguna función de cuenta ni de órdenes.

## Paso 2: Probar en tu PC (Windows)

En una terminal, dentro de la carpeta del proyecto:

```bat
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
```

Generá el token secreto del conector:

```bat
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Abrí `.env` y completá `PPI_API_KEY`, `PPI_API_SECRET` y `MCP_TOKEN` (el valor que acabás de generar). Después corré:

```bat
python probar_ppi.py
```

Tiene que mostrar los tipos y plazos válidos, la cotización de GGAL y el MEP con AL30, y terminar con **"Todo OK"**.

> El plazo por defecto es `A-24HS`. Si PPI lo rechaza, mirá en la salida la lista de plazos válidos y cambiá `PLAZO_DEFAULT` al principio de `server.py`. Lo mismo para los tipos de instrumento (`ACCIONES`, `BONOS`, etc.).

## Paso 3: Subir el código a GitHub

1. Creá un repositorio **privado** en GitHub (por ejemplo `ppi-mcp`).
2. Subí todos los archivos **menos `.env`** y la carpeta `.venv`. Desde la web podés usar "uploading an existing file" y arrastrarlos.

## Paso 4: Publicarlo en Render

1. Creá una cuenta en render.com y conectala con GitHub.
2. Creá un **Web Service** nuevo a partir del repositorio `ppi-mcp`.
3. Configuración:
   - Runtime: **Python**
   - Build command: `pip install -r requirements.txt`
   - Start command: `python server.py`
4. En **Environment**, cargá las variables `PPI_API_KEY`, `PPI_API_SECRET` y `MCP_TOKEN`, con los mismos valores de tu `.env`.
5. Elegí el plan:
   - El **gratuito** apaga el servicio después de unos 15 minutos sin uso. La primera consulta posterior tarda casi un minuto y puede fallar; reintentá.
   - El **pago más chico** queda siempre encendido.
6. Cuando termine el deploy, abrí `https://TU-SERVICIO.onrender.com/` en el navegador. Tiene que decir `ok`.

## Paso 5: Agregar el conector en Claude

1. En Claude, andá a la sección de **Conectores** y elegí **agregar un conector personalizado**.
2. Pegá esta URL:

   ```
   https://TU-SERVICIO.onrender.com/mcp/TU_MCP_TOKEN
   ```

3. Activalo en el chat donde lo quieras usar.

Ejemplos de pedidos:

- *"¿A cuánto está GGAL y AL30 en PPI?"*
- *"Dame el MEP y el CCL con AL30 y con GD30."*
- *"Verificá estos precios de operaciones de hoy contra el mercado: ..."*
- *"TIR del AL30 a 84.000 pesos."*

---

## Seguridad

- **La URL del conector es la llave.** Quien la tenga puede consultar precios con tus claves de PPI. No la compartas.
- Si se filtra, cambiá `MCP_TOKEN` en Render (se vuelve a publicar solo) y actualizá la URL en Claude.
- Las claves de PPI viven solo en las variables de entorno de Render y en tu `.env` local.
- El servidor no tiene funciones para operar ni para ver saldos o tenencias.
- El log de accesos está apagado para que la URL secreta no quede registrada en el hosting.

## Problemas comunes

| Síntoma | Qué revisar |
|---|---|
| "PPI rechazó el login" | Claves mal copiadas o vencidas. Probá de nuevo con `probar_ppi.py`. |
| Error de tipo o plazo | Hay que usar los valores exactos de `valores_validos`. Claude los consulta solo cuando PPI rechaza un valor. |
| Claude no ve las herramientas | URL incompleta: tiene que terminar en `/mcp/` + tu token. |
| Primera consulta lenta o con timeout | Plan gratuito de Render "despertando". Reintentá o pasá al plan pago. |
| `https://TU-SERVICIO.onrender.com/` no dice `ok` | Revisá los logs del deploy en Render. Lo más común es que falte una variable de entorno. |
