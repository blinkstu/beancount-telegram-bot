from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Iterable


class FavaManager:
    """Manage a single Fava subprocess that serves all user ledgers."""

    def __init__(self, ledger_root: Path, host: str = "0.0.0.0", port: int = 5001):
        self._ledger_root = ledger_root.resolve()
        self._host = host
        self._port = port
        self._process: asyncio.subprocess.Process | None = None
        self._lock = asyncio.Lock()
        self._current_ledgers: set[Path] = set()
        self._logger = logging.getLogger(__name__)

    async def start(self) -> None:
        async with self._lock:
            await self._restart_if_needed()

    async def refresh(self) -> None:
        async with self._lock:
            await self._restart_if_needed()

    async def stop(self) -> None:
        async with self._lock:
            await self._stop_process()

    def _discover_ledgers(self) -> set[Path]:
        patterns: Iterable[str] = ("*.bean", "*.beancount")
        ledgers: set[Path] = set()
        for pattern in patterns:
            ledgers.update(p.resolve() for p in self._ledger_root.glob(pattern) if p.is_file())
        return ledgers

    async def _restart_if_needed(self) -> None:
        self._ledger_root.mkdir(parents=True, exist_ok=True)
        ledgers = self._discover_ledgers()
        if not ledgers:
            await self._stop_process()
            self._current_ledgers = set()
            return

        if (
            ledgers == self._current_ledgers
            and self._process is not None
            and self._process.returncode is None
        ):
            return

        await self._stop_process()
        try:
            ledgers_sorted = sorted(ledgers)
            self._logger.info(
                "Starting Fava on %s:%s with ledgers: %s",
                self._host,
                self._port,
                ", ".join(map(str, ledgers_sorted)),
            )
            self._process = await asyncio.create_subprocess_exec(
                "fava",
                "--host",
                self._host,
                "--port",
                str(self._port),
                *[str(path) for path in ledgers_sorted],
                cwd=str(self._ledger_root),
            )
            self._current_ledgers = ledgers
        except FileNotFoundError:
            self._logger.error(
                "Could not start Fava: command 'fava' not found. Install Fava to enable the web UI."
            )
            self._current_ledgers = set()
        except Exception as exc:  # noqa: BLE001
            self._logger.exception("Failed to start Fava: %s", exc)
            self._current_ledgers = set()

    async def _stop_process(self) -> None:
        if self._process is None:
            return
        if self._process.returncode is None:
            self._logger.info("Stopping Fava process (pid=%s)", self._process.pid)
            self._process.terminate()
            try:
                await asyncio.wait_for(self._process.wait(), timeout=5)
            except asyncio.TimeoutError:
                self._logger.warning("Fava process did not exit gracefully; killing.")
                self._process.kill()
                await self._process.wait()
        self._process = None
