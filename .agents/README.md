# .agents — Equipo de agentes

Fuente única de verdad de cada rol: **modelo, permisos, contexto, entradas, salidas y prompt**.
El orquestador (T3 Code) lee el archivo del rol y lanza el agente con esos parámetros.
`AGENTS.md` (raíz) contiene las reglas del repo que aplican a **todos** los agentes que escriben código.

## Equipo

| Rol | Archivo | Proveedor / modelo | Effort | Escribe | Salida |
|-----|---------|--------------------|--------|---------|--------|
| PM / Orquestador | [pm.md](pm.md) | claudeAgent / `claude-opus-5-5` | default | `docs/`, `.agents/`, `README.md`, git/PRs | backlog, PRs, merge |
| Coder | [coder.md](coder.md) | opencode / `opencode-go/deepseek-v4.1-flash` | `high` | archivos de la historia | `CODER_REPORT` |
| QA | [qa.md](qa.md) | opencode / `opencode-go/deepseek-v4.1-flash` | `high` | solo `tests/` | `QA_REPORT` (JSON) |
| Code Reviewer | [code-reviewer.md](code-reviewer.md) | opencode / `opencode-go/glm-5.3-flash` | `high` | nada (solo lectura) | `REVIEW` (JSON) |
| Security Reviewer | [security-reviewer.md](security-reviewer.md) | claudeAgent / `claude-opus-5-5` | `medium` | nada (solo lectura) | `REVIEW` (JSON) |

## Pipeline por historia

```
            ┌────────────────────────────── fix loop (máx. 3 rondas) ──────────────────────────────┐
            ▼                                                                                       │
PM ──▶ CODER ──▶ gate: make check ──▶ QA ──▶ gate: make check ──▶ CODE REVIEW ∥ SECURITY REVIEW ──┤
                                                                                                    │
                                              ambos APPROVE (sin blocker/major) ──▶ PM: commit + PR + CI ──▶ squash merge
```

| Etapa | Entrada | Gate de salida | Si falla |
|-------|---------|----------------|----------|
| Coder | historia + (hallazgos de la ronda anterior) | `make check` verde, verificado por el PM | se reintenta con el error (cuenta como ronda) |
| QA | historia + diff | `make check` verde; los bugs de `app/` van al reporte | los bugs vuelven al Coder |
| Reviews (en paralelo) | historia + diff + `CODER_REPORT` + `QA_REPORT` | `verdict == "APPROVE"` en ambas | los hallazgos blocker/major vuelven al Coder |
| PM | todos los reportes | CI verde | — |

**Escalamiento al usuario:** 3 rondas sin converger · ambigüedad de spec que bloquea · un reviewer
pide cambiar un contrato documentado.

## Principios de diseño del equipo

- **Separación de poderes:** quien escribe no aprueba. Los reviewers no tienen permisos de escritura;
  QA no puede tocar `app/` (así no "arregla" el código para que pase su propio test).
- **Modelos por costo/valor:** generación de código, QA y code review → modelos rápidos y baratos
  (DeepSeek V4.1 Flash, GLM 5.3 Flash; un modelo distinto al del coder revisa su código).
  Seguridad → Opus (effort medium), donde un error cuesta más. Nada corre con Opus effort high
  por costo de tokens. Cambiar un modelo = editar el frontmatter del rol.
- **Contexto mínimo y explícito:** cada rol tiene una lista `reads` acotada. Los reviewers miran el
  **diff**, no el repo entero. Menos tokens y menos alucinaciones.
- **Salidas estructuradas:** los reportes llevan un bloque JSON con schema fijo → el orquestador decide
  sin interpretar prosa.
- **Gates verificados por el orquestador:** "make check pasó" nunca se toma de palabra; el PM lo
  re-ejecuta.
- **Agentes stateless:** cada ronda es un agente nuevo que recibe todo lo que necesita en el prompt
  (historia, diff, hallazgos). Nada depende de la memoria de una corrida anterior.
- **Trazabilidad:** los reportes y veredictos quedan en la descripción y los comentarios del PR.

## Formato de cada archivo de rol

```yaml
---
role: <id>
provider: claudeAgent | opencode      # instancia de proveedor en T3 Code
model: <model id>
options: { effort | variant: ... }
writes: [globs]                        # lo único que puede modificar
reads: [paths]                         # contexto a leer primero (en orden)
inputs: [variables del template]
output: <nombre del contrato>
max_rounds: <n>
---
# Prompt del rol (template con {variables})
```

## Integración nativa (opcional)

Claude Code (`.claude/agents/`) y OpenCode (`.opencode/agent/`) usan frontmatter propio. Si se
quiere descubrimiento nativo, se generan desde estos archivos; no se mantienen a mano.
