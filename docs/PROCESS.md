# Proceso de desarrollo

SDLC con un equipo de agentes de IA. Las definiciones de cada rol (modelo, permisos, contexto,
prompt y contrato de salida) están en **[`.agents/`](../.agents/README.md)**; este documento describe el
proceso de ingeniería alrededor de ellos.

## Fases

| Fase | Responsable | Artefacto |
|------|-------------|-----------|
| Requerimientos | PM ([pm.md](../.agents/pm.md)) | [REQUIREMENTS.md](REQUIREMENTS.md): preguntas al usuario, supuestos y decisiones |
| Planificación | PM | [BACKLOG.md](BACKLOG.md): historias con criterios de aceptación |
| Especificación | PM | DATA_MODEL / API / JOBS / TESTING (contratos) |
| Diseño visual | — | fuera de alcance (no hay UI) |
| Desarrollo | Coder ([coder.md](../.agents/coder.md)) | código + tests, `CODER_REPORT` |
| Testing | QA ([qa.md](../.agents/qa.md)) | tests de borde, `QA_REPORT` |
| Review | Code + Security ([code-reviewer.md](../.agents/code-reviewer.md), [security-reviewer.md](../.agents/security-reviewer.md)) | `REVIEW` JSON |
| Integración | PM + GitHub Actions (S8) | PR, CI verde, squash merge |

## Convenciones de git

- Branch por historia: `feat/<Sx>-<slug>` desde `origin/main`.
- Conventional Commits; un commit squash por historia en `main`:
  `feat(alerts): S4 alerts CRUD API`. Sin trailers de atribución (`Co-Authored-By`).
- Descripción del PR = `.github/pull_request_template.md` con los reportes de QA y las reviews.
- `main` siempre en verde. Hasta que exista el CI (S8), el gate es `make check`, re-ejecutado por el PM.

## Severidades

| Severidad | Significado | ¿Bloquea el merge? |
|-----------|-------------|--------------------|
| blocker | bug, incumplimiento de un criterio o contrato, vulnerabilidad que expone datos de otro usuario | sí |
| major | riesgo real de producción (race, DoS, error no manejado, falta de índice en un path caliente) | sí |
| minor | hardening / mejora sin escenario de falla concreto | no (follow-up en el PR) |
| nit | preferencia | no |

## Escalamiento al usuario

- 3 rondas de fixes sin converger.
- Una ambigüedad de spec que bloquea el avance.
- Una propuesta de cambiar un contrato documentado o una decisión de producto.
