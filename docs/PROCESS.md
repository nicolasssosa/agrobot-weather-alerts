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
| Integración | PM + GitHub Actions (S16) | PR, CI verde, squash merge |

## Convenciones de git

- Branch por historia: `feat/<Sx>-<slug>` desde `origin/main`.
- Conventional Commits; un commit squash por historia en `main`:
  `feat(alerts): S4 alerts CRUD API`. Sin atribución de IA ni en commits (`Co-Authored-By`) ni en PRs.
- Descripción del PR = `.github/pull_request_template.md` con los reportes de QA y las reviews.
- `main` siempre en verde. El gate es el CI de GitHub Actions (S16) más `make check` re-ejecutado por el PM.

## Merge gate (antes de cada merge)

Con QA y reviews aprobadas, el PM clasifica el cambio y escribe el **resumen de lo que se sube**
(va arriba en la descripción del PR y se le muestra al usuario):

```
Tipo:        feat | fix | refactor | test | docs | ci | chore   (Conventional Commits)
Criticidad:  🔴 Crítica | 🟡 Media | 🟢 Baja
Revisión humana: requerida | no requerida  (+ motivo)
Qué se sube: comportamiento nuevo o cambiado, en 3 a 5 bullets
Impacto:     esquema de DB / API pública / seguridad / jobs / CI
Riesgos y rollback: qué puede salir mal y cómo se revierte
```

### Criterios de criticidad (gana el nivel más alto que aplique)

| Nivel | Se dispara si el cambio… | ¿Revisión humana? | Merge |
|-------|--------------------------|-------------------|-------|
| 🔴 Crítica | agrega o modifica **migraciones** de esquema · toca **auth, ownership o permisos** · cambia un **contrato público** (paths, status codes, tablas) o una decisión de producto · toca **CI/CD, Docker o secretos** · agrega **dependencias** · cambia la lógica de **envío de notificaciones** (dedupe, reintentos: impacta a usuarios reales) · tuvo un hallazgo blocker/major **de seguridad o de integridad de datos** corregido en la última ronda · llegó a 3 rondas · el Coder reportó desvíos de contrato | **Sí** | el PM frena, presenta el resumen y espera el OK explícito del usuario |
| 🟡 Media | agrega funcionalidad nueva aditiva, cubierta por tests, con ambas reviews en APPROVE sin desvíos de contrato (un bug funcional/performance ya corregido y re-verificado no sube el nivel) | No | automático; el usuario recibe el resumen |
| 🟢 Baja | solo docs, solo tests, o refactor sin cambio de comportamiento | No | automático |

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
