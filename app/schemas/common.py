"""Schemas compartidos.

TODO(S2): implementar según docs/API.md §Convenciones:
- `class Page[T](BaseModel)`: items: list[T], limit: int, offset: int
- `class PaginationParams`: dependency con `limit: int = Query(50, ge=1, le=100)`,
  `offset: int = Query(0, ge=0)`.
"""
