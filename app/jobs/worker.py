"""Proceso del background job: `python -m app.jobs.worker [--once]`. TODO(S7).

Spec completa: docs/JOBS.md §Worker.

@dataclass(frozen=True, slots=True)
class CycleResult:
    created: int
    dispatch: DispatchResult

async def run_cycle(sessionmaker: async_sessionmaker[AsyncSession], notifier: Notifier,
                    settings: Settings, *, now: datetime | None = None) -> CycleResult
async def run_forever(sessionmaker, notifier, settings, stop_event: asyncio.Event) -> None
def main(argv: Sequence[str] | None = None) -> None
"""

if __name__ == "__main__":
    raise SystemExit("TODO(S7): implementar main()")
