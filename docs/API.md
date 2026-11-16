# API REST

Base: `/api/v1` (excepto `/health`). JSON en todos lados. OpenAPI en `/docs`.

## Convenciones

- **Autenticación (simulada):** header `X-User-Id: <int>`. Lo resuelve `get_current_user`
  (`app/api/deps.py`). Ausente, no numérico o usuario inexistente → **401**
  `{"detail": "Missing or invalid X-User-Id header"}`. En producción se reemplaza por la identidad
  que entregue el gateway de WhatsApp (número de teléfono → user). Solo `POST /users` y `/health`
  no lo requieren.
- **Ownership:** un recurso de otro usuario responde **404** (igual que si no existiera).
- **Errores:** `{"detail": "..."}`. 404 `NotFoundError`, 409 `ConflictError`, 422 validación de FastAPI.
- **Paginación** en listados: query `limit` (1..100, default 50) y `offset` (≥0, default 0).
  Respuesta: `{"items": [...], "limit": 50, "offset": 0}` (schema genérico `Page[T]`).
- **Fechas:** `date` ISO (`2026-11-20`); `datetime` ISO 8601 con zona.
- Requests con campos desconocidos → 422 (`extra="forbid"`).

---

## health

### `GET /health`
Ejecuta `SELECT 1` contra la base.
- 200 `{"status": "ok"}`
- 503 `{"status": "unavailable"}` si la query falla (capturar la excepción, loguearla).

## users

### `POST /api/v1/users` — sin auth
Request `UserCreate`:
```json
{"phone_number": "+5491122334455", "name": "Juan Pérez"}
```
- `phone_number`: regex E.164 `^\+[1-9]\d{7,14}$`.
- `name`: 1..120 chars, se aplica `strip()`; vacío tras strip → 422.

Respuesta **201** `UserRead`: `{"id", "phone_number", "name", "created_at"}`.
409 si el teléfono ya existe.

### `GET /api/v1/users/me`
200 `UserRead` del usuario autenticado.

## fields

### `POST /api/v1/fields`
Request `FieldCreate`:
```json
{"name": "Lote Norte", "latitude": -34.6037, "longitude": -58.3816}
```
- `name` 1..120 (strip), `latitude` [-90, 90], `longitude` [-180, 180] (`float` en el schema).

Respuesta **201** `FieldRead`: `{"id", "name", "latitude", "longitude", "created_at"}`.
409 si el usuario ya tiene un campo con ese nombre.

### `GET /api/v1/fields` → 200 `Page[FieldRead]`
### `GET /api/v1/fields/{field_id}` → 200 `FieldRead` | 404

## forecasts

### `GET /api/v1/fields/{field_id}/forecasts`
Query (todas opcionales): `date_from: date`, `date_to: date`, `event_type: EventType`.
- `date_from > date_to` → 422 `{"detail": "date_from must be <= date_to"}` (lanzar `HTTPException(422)` en el router).
- Sin paginación (como mucho ~días × 5 eventos).

Respuesta 200: `list[ForecastRead]`:
```json
[{"forecast_date": "2026-11-17", "event_type": "frost", "probability": 72,
  "source": "mock", "fetched_at": "2026-11-16T12:00:00Z"}]
```
404 si el campo no existe / no es del usuario.

## alerts

### `POST /api/v1/alerts`
Request `AlertCreate`:
```json
{"field_id": 1, "event_type": "frost", "threshold": 70}
```
- `event_type`: uno de `frost | rain | hail | strong_wind | heat_wave` (otro valor → 422).
- `threshold`: int 1..100.
- `is_active`: no se envía; nace en `true`.

Respuesta **201** `AlertRead`:
```json
{"id": 1, "field_id": 1, "event_type": "frost", "threshold": 70, "is_active": true,
 "created_at": "...", "updated_at": "..."}
```
- 404 `Field not found` si el campo no es del usuario.
- 409 si ya hay una alerta para (campo, evento).

### `GET /api/v1/alerts` → 200 `Page[AlertRead]`
Filtros opcionales: `field_id`, `event_type`, `is_active`.

### `GET /api/v1/alerts/{alert_id}` → 200 `AlertRead` | 404

### `PATCH /api/v1/alerts/{alert_id}`
Request `AlertUpdate` (todos opcionales, al menos uno requerido → si viene `{}` responder 422
con un `model_validator`):
```json
{"threshold": 80, "is_active": false}
```
`field_id` y `event_type` no son editables (crear otra alerta). 200 `AlertRead` | 404.

### `DELETE /api/v1/alerts/{alert_id}` → 204 sin body | 404

## notifications

### `GET /api/v1/notifications` → 200 `Page[NotificationRead]`
Filtros opcionales: `status` (`pending|sent|failed`), `alert_id`.
```json
{"id": 10, "alert_id": 1, "forecast_date": "2026-11-18", "event_type": "frost",
 "probability": 85, "threshold": 70, "status": "sent", "attempts": 1,
 "sent_at": "...", "created_at": "..."}
```
(`last_error` y `next_attempt_at` no se exponen: son internos.)

---

## Ejemplo end-to-end (curl)

```bash
curl -s -X POST localhost:8000/api/v1/users -H 'content-type: application/json' \
  -d '{"phone_number":"+5491100000001","name":"Ana"}'
curl -s -X POST localhost:8000/api/v1/fields -H 'X-User-Id: 1' -H 'content-type: application/json' \
  -d '{"name":"Lote Norte","latitude":-34.6,"longitude":-58.4}'
curl -s -X POST localhost:8000/api/v1/alerts -H 'X-User-Id: 1' -H 'content-type: application/json' \
  -d '{"field_id":1,"event_type":"frost","threshold":60}'
make ingest && make jobs-once
curl -s 'localhost:8000/api/v1/notifications?status=sent' -H 'X-User-Id: 1'
```
