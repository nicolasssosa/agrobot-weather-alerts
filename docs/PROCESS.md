# Proceso de desarrollo multi-agente

El trabajo se organiza como un SDLC normal, con cada rol cubierto por un agente.
Lo coordina un **orquestador** (Claude Opus 5.5), que además actúa de PM.

## Roles

| Rol | Modelo | Responsabilidad | Escribe código |
|-----|--------|-----------------|----------------|
| PM / Docs (orquestador) | Claude Opus 5.5 | Requerimientos, backlog, criterios de aceptación, README; abre las branches y los PRs y mergea | Solo docs |
| Coder | DeepSeek V4.1 Flash (OpenCode) | Implementa la historia + tests (`AGENTS.md`) | ✅ |
| QA | DeepSeek V4.1 Flash (OpenCode) | Verifica criterios de aceptación, corre `make check`, agrega tests de borde faltantes | Solo tests |
| Code Reviewer | Claude Opus 5.5 (effort medium) | Corrección, modelo de datos, asincronía, adherencia a contratos | ❌ |
| Security Reviewer | Claude Opus 5.5 | OWASP API Top 10, ownership/IDOR, SQLi, validación, secretos, logs, Docker/CI | ❌ |

Diseño: fuera de alcance (no hay UI).

## Flujo por historia

```
PM: branch feat/Sx-slug desde main
  └─▶ Coder (DeepSeek) implementa + tests ──▶ make check verde
        └─▶ QA (DeepSeek) verifica criterios de aceptación / agrega tests de borde
              └─▶ en paralelo: Code Review (Opus medium) + Security Review (Opus)
                    ├─ ambos aprueban ─▶ PM: commit, push, PR con el resumen de las reviews ─▶ CI verde ─▶ squash merge
                    └─ hay hallazgos  ─▶ vuelve al Coder con los hallazgos (máx. 3 rondas; si no converge, escala al usuario)
```

- Las historias son **secuenciales** (cada una depende de la anterior): una sola en vuelo a la vez.
- Commits con Conventional Commits: `feat(alerts): ...`, `test(...)`, `ci: ...`, `docs: ...`.
- Hasta que exista el CI (S8), "CI verde" = `make check` local corrido por el orquestador.

## Severidad de hallazgos (reviews)

- **blocker** — bug, incumplimiento de un criterio de aceptación o de un contrato, o vulnerabilidad explotable → bloquea el merge.
- **major** — riesgo real de producción (race condition, falta de índice, error no manejado) → bloquea el merge.
- **minor / nit** — estilo y mejoras → no bloquea; se anota en el PR.

Formato de cada hallazgo: `[severidad] path:línea — problema — escenario concreto — fix sugerido`.

## Prompts de rol (los usa el orquestador)

### Coder
> Sos el coder de la historia **{Sx}**. Leé `AGENTS.md`, luego `docs/BACKLOG.md` §{Sx} y los docs
> que referencia. Implementá exclusivamente esa historia con sus tests. Corré `make check` hasta que
> esté en verde. No hagas commit. Terminá con el reporte final de `AGENTS.md`.
> {si hay ronda de fixes: "Hallazgos a corregir: ..."}

### QA
> Sos QA de la historia **{Sx}**. No modifiques código de `app/`. Por cada criterio de aceptación
> de `docs/BACKLOG.md` §{Sx} y cada caso de `docs/TESTING.md` aplicable, verificá que exista un test
> que lo cubra; si falta, agregalo en `tests/`. Corré `make check`. Si un test nuevo falla por un bug
> de `app/`, **no lo arregles**: reportalo como hallazgo. Respondé con una tabla criterio → test →
> estado, más la lista de bugs.

### Code Reviewer
> Revisá el diff `git diff main...HEAD` de la historia **{Sx}** contra `docs/` (contratos) y
> `AGENTS.md`. Foco: corrección, casos borde, transacciones/commits, uso correcto de async,
> integridad del modelo, queries eficientes (N+1, índices), tests que realmente prueben el criterio.
> No edites archivos. Devolvé los hallazgos con el formato de severidad y un veredicto:
> `APPROVE` o `REQUEST_CHANGES`.

### Security Reviewer
> Hacé una security review del diff `git diff main...HEAD` de la historia **{Sx}**. Foco:
> autorización y ownership (IDOR/BOLA), SQL injection, validación de input y mass assignment, fuga de
> información en errores o logs (PII: teléfonos), DoS (paginación sin límite, queries sin cota),
> secretos, configuración de Docker/CI (usuario root, permisos de token, acciones sin pin). Solo
> hallazgos con un escenario de explotación concreto. No edites archivos. Veredicto:
> `APPROVE` o `REQUEST_CHANGES`.

## Trazabilidad

`REQUIREMENTS.md` (RF/RNF) → `BACKLOG.md` (historia + criterios) → branch/PR `feat/Sx-*`
(cuerpo del PR: criterios ✔, resumen de QA, veredictos de las reviews) → commit squash en `main`.
