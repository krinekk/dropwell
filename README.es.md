# drop

[English](README.md) | [Español](README.es.md)

[![CI](https://github.com/ericbosch/drop/actions/workflows/test.yml/badge.svg)](https://github.com/ericbosch/drop/actions/workflows/test.yml)
[![Python](https://img.shields.io/badge/python-3.12%2B-blue.svg)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-005571?logo=fastapi)](https://fastapi.tiangolo.com/)
[![PostgreSQL](https://img.shields.io/badge/PostgreSQL-4169E1?logo=postgresql&logoColor=white)](https://www.postgresql.org/)
[![Licencia: MIT](https://img.shields.io/badge/licencia-MIT-green.svg)](LICENSE)

`drop` es una pequeña API autenticada para capturar notas personales, eventos de
automatización y flujos de inbox de baja fricción.

Acepta payloads UTF-8 en endpoints por tema, los guarda en PostgreSQL y expone
una superficie mínima para listar, actualizar, archivar y borrar capturas. Es
deliberadamente aburrida: sin ranking de feed, sin claims de IA, sin magia en
segundo plano, sin teatro de producto.

> Proyecto personal. No afiliado con mi empleador.

## Por Qué Existe

Muchos sistemas de automatización personal necesitan una primitiva aburrida:

1. recibir algo rápido
2. guardarlo de forma durable
3. revisarlo o archivarlo después
4. evitar acoplar los productores al resto del sistema

`drop` es esa primitiva. Los productores solo necesitan HTTP y un bearer token.
La clasificación, enriquecimiento, memoria y capas de agentes pueden vivir en
otro sitio.

## Estado

Estado actual:

- Endpoint autenticado de escritura: `POST /drop/{topic}`
- Endpoint autenticado de lectura: `GET /drops`
- Endpoint autenticado de actualización: `PATCH /drops/{id}`
- Endpoint autenticado de borrado: `DELETE /drops/{id}`
- Persistencia en PostgreSQL
- CI con ruff y pytest
- Adaptador serverless para Vercel
- Ejemplo opcional de servicio local con `systemd`

No incluido:

- Cuentas multiusuario
- OAuth
- Ingesta pública sin autenticación
- UI/dashboard
- Procesamiento con IA
- Workers en segundo plano

## Qué Demuestra

Como proyecto de portfolio, `drop` busca demostrar algunos instintos backend
antes que una gran superficie de producto:

- una frontera HTTP pequeña con autenticación explícita
- persistencia PostgreSQL con helpers de acceso a datos enfocados
- validación de entrada y límites de request claros
- CI contra un servicio PostgreSQL real
- documentación operativa para despliegue local y serverless
- notas de seguridad que explican los trade-offs en lugar de ocultarlos

## API

| Método | Ruta | Auth | Descripción |
|---|---|---|---|
| `GET` | `/health` | No | Health check y versión |
| `POST` | `/drop/{topic}` | Sí | Guarda un payload UTF-8 bajo un tema |
| `GET` | `/drops` | Sí | Lista capturas |
| `PATCH` | `/drops/{id}` | Sí | Actualiza `status` y/o `body` |
| `DELETE` | `/drops/{id}` | Sí | Borra una captura |

Los temas deben cumplir:

```text
[a-z0-9][a-z0-9-]{0,63}
```

Estados:

- `inbound`
- `archived`

Tamaño máximo por defecto:

- `10 MiB`, configurable con `DROP_MAX_BODY_BYTES`

## Quickstart

Requisitos:

- Python 3.12+
- `uv`
- PostgreSQL

```bash
git clone https://github.com/ericbosch/drop
cd drop
cp .env.example .env
docker compose up -d postgres
uv sync --extra dev
uv run uvicorn drop.app:app --host 127.0.0.1 --port 9731
```

`docker compose up -d postgres` arranca un contenedor local de PostgreSQL 16
y crea las bases de datos `drop` y `drop_test` que usan la app y la suite de
tests. No hace falta instalar PostgreSQL en local.

Edita `.env` antes de arrancar el servicio:

```env
DROP_TOKEN=replace-with-a-long-random-token
DROP_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/drop
DROP_PORT=9731
DROP_MAX_BODY_BYTES=10485760
DROP_CORS_ORIGINS=http://localhost:3000
```

## Uso

Usa `http://127.0.0.1:9731` en local o sustitúyelo por tu propia URL de
despliegue.

```bash
export DROP_URL="http://127.0.0.1:9731"
export DROP_TOKEN="replace-with-a-long-random-token"  # debe coincidir con el valor en .env
```

Health check:

```bash
curl "$DROP_URL/health"
```

Capturar una nota:

```bash
curl -X POST "$DROP_URL/drop/note" \
  -H "Authorization: Bearer $DROP_TOKEN" \
  --data "remember to review the API boundary"
```

Capturar JSON como body crudo:

```bash
curl -X POST "$DROP_URL/drop/github-event" \
  -H "Authorization: Bearer $DROP_TOKEN" \
  -H "Content-Type: application/json" \
  --data '{"action":"push","repo":"drop"}'
```

Listar capturas:

```bash
curl "$DROP_URL/drops?limit=10" \
  -H "Authorization: Bearer $DROP_TOKEN"
```

Ejemplo de respuesta de listado:

```json
[
  {
    "id": "65cc274b-a368-455b-a6c1-cf3a3f9d5b81",
    "topic": "note",
    "body": "remember to review the API boundary",
    "received_at": "2026-05-28T00:00:00+00:00",
    "updated_at": "2026-05-28T00:00:00+00:00",
    "status": "inbound"
  }
]
```

Filtrar por tema o estado:

```bash
curl "$DROP_URL/drops?topic=note&status=inbound" \
  -H "Authorization: Bearer $DROP_TOKEN"
```

Archivar una captura:

```bash
curl -X PATCH "$DROP_URL/drops/<id>" \
  -H "Authorization: Bearer $DROP_TOKEN" \
  -H "Content-Type: application/json" \
  --data '{"status":"archived"}'
```

Actualizar el texto del body:

```bash
curl -X PATCH "$DROP_URL/drops/<id>" \
  -H "Authorization: Bearer $DROP_TOKEN" \
  -H "Content-Type: application/json" \
  --data '{"body":"cleaned up note body"}'
```

Borrar una captura:

```bash
curl -X DELETE "$DROP_URL/drops/<id>" \
  -H "Authorization: Bearer $DROP_TOKEN"
```

Respuesta correcta de captura:

```json
{
  "id": "65cc274b-a368-455b-a6c1-cf3a3f9d5b81",
  "topic": "note",
  "received_at": "2026-05-28T00:00:00+00:00",
  "updated_at": "2026-05-28T00:00:00+00:00"
}
```

## Configuración

| Variable | Requerida | Por defecto | Descripción |
|---|---:|---|---|
| `DROP_TOKEN` | Sí | - | Bearer token para todos los endpoints salvo health |
| `DROP_DATABASE_URL` | Sí | - | Connection string de PostgreSQL |
| `DROP_HOST` | No | `127.0.0.1` | Host usado por helpers de proceso local |
| `DROP_PORT` | No | `9731` | Puerto usado por helpers de proceso local |
| `DROP_MAX_BODY_BYTES` | No | `10485760` | Tamaño máximo aceptado por request |
| `DROP_CORS_ORIGINS` | No | vacío | Origins de navegador permitidos por CORS, separados por coma |

## Desarrollo

Instalar dependencias:

```bash
uv sync --extra dev
```

Arrancar la API:

```bash
uv run uvicorn drop.app:app --host 127.0.0.1 --port 9731 --reload
```

Ejecutar linting:

```bash
uv run ruff check .
```

Ejecutar tests:

```bash
uv run pytest
```

Los tests usan PostgreSQL. Por defecto esperan:

```text
postgresql://postgres:postgres@localhost:5432/drop_test
```

`docker compose up -d postgres` (ver Quickstart) crea esta base de datos
automáticamente. Puedes sobrescribirlo con:

```bash
export TEST_DROP_DATABASE_URL="postgresql://user:password@host:5432/drop_test"
uv run pytest
```

## Despliegue

El repo incluye dos caminos orientados a despliegue:

- `api/index.py` para despliegue serverless estilo Vercel mediante Mangum.
- `deploy/drop.service` para un servicio local de usuario con `systemd`.

Configura estas variables de entorno en el entorno de despliegue:

```bash
DROP_TOKEN=<long-random-token>
DROP_DATABASE_URL=<postgres-url>
DROP_MAX_BODY_BYTES=10485760
DROP_CORS_ORIGINS=https://your-ui.example.com
```

Para Vercel:

```bash
vercel env add DROP_TOKEN production
vercel env add DROP_DATABASE_URL production
vercel --prod
```

Usa tu propia URL de despliegue en ejemplos y documentación. No commitees
tokens reales, URLs de base de datos, hostnames locales o endpoints de
producción.

## Arquitectura

```text
producer scripts / webhooks / tools
        |
        | HTTP + bearer token
        v
FastAPI app
        |
        v
PostgreSQL table: drop
        |
        v
review / archive / downstream automation
```

Decisiones de diseño:

- Usar una frontera HTTP simple y autenticada.
- Guardar body text UTF-8 crudo sin intentar inferir significado.
- Hacer barata la integración de productores.
- Mantener el enriquecimiento downstream fuera de este servicio.
- Preferir primitivas operativas aburridas antes que automatización ingeniosa.

## Seguridad Y Privacidad

- Todos los endpoints salvo health requieren bearer token.
- El token debe configurarse mediante variables de entorno.
- El proyecto no incluye gestión de cuentas ni permisos por tema.
- Los payloads se guardan como texto crudo. No envíes secretos salvo que tu
  despliegue, base de datos, backups y política de retención estén diseñados
  para ello.
- Mantén `.env`, ficheros de base de datos, logs y metadata de despliegue fuera
  de Git.
- Revisa `SECURITY.md` antes de exponer una instancia más allá de localhost.

## Relación Con KOS

`drop` puede usarse como primitiva de ingesta para KOS u otros sistemas de
automatización personal, pero es intencionadamente independiente.

KOS es un proyecto personal experimental a largo plazo. `drop` no debe sugerir
que KOS sea un producto comercial, un proyecto del empleador o un sistema listo
para mercado.

## Roadmap

Posibles siguientes pasos:

- Cursor opcional de paginación
- Allowlist opcional de temas
- Endpoint básico de métricas
- Ejemplos OpenAPI mínimos
- Historia más explícita de retención/export

No objetivos salvo que cambie la dirección del proyecto:

- Convertir `drop` en un SaaS
- Añadir resumen con IA dentro de la API de captura
- Construir un inbox social o colaborativo
- Reemplazar un task manager completo

## Contribuir

Este es principalmente un proyecto de infraestructura personal, pero se aceptan
issues y pull requests pequeños si mantienen el proyecto simple, seguro y
aburrido.

Ver `CONTRIBUTING.md`.

## Licencia

MIT. Ver `LICENSE`.
