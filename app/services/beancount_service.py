from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import datetime
from decimal import Decimal
from pathlib import Path
from typing import Iterable, Union

from beancount import loader
from beancount.core import realization
from beancount.core.amount import Amount
from beancount.core.inventory import Inventory

try:
    from beancount.query import query as bquery  # type: ignore
except ImportError:  # pragma: no cover - optional dependency
    bquery = None  # type: ignore[arg-type, assignment]

from ..config import get_settings


@dataclass
class BeancountService:
    root: Path

    @classmethod
    def from_settings(cls) -> "BeancountService":
        settings = get_settings()
        return cls(root=settings.beancount_root.resolve())

    def user_ledger_path(self, user_id: str) -> Path:
        return self.root / f"{user_id}.bean"

    def append_entries(self, user_id: str, entries: list[str]) -> Path:
        ledger_path = self.user_ledger_path(user_id)
        ledger_path.parent.mkdir(parents=True, exist_ok=True)
        cleaned_entries = [self._normalize_entry(entry) for entry in entries if entry.strip()]
        existing_content = ledger_path.read_text(encoding="utf-8") if ledger_path.exists() else ""
        final_content = self._compose_content(existing_content, cleaned_entries)
        with ledger_path.open("w", encoding="utf-8") as ledger_file:
            ledger_file.write(final_content)
        return ledger_path

    def summarize_accounts(self, user_id: str) -> tuple[list[str], list[str]]:
        ledger_path = self.user_ledger_path(user_id)
        if not ledger_path.exists() or ledger_path.stat().st_size == 0:
            return [], []

        existing_content = ledger_path.read_text(encoding="utf-8")
        updated_content = self._compose_content(existing_content, [])
        if updated_content != existing_content:
            ledger_path.write_text(updated_content, encoding="utf-8")

        entries, errors, options_map = loader.load_file(str(ledger_path))
        root_account = realization.realize(entries, options_map)

        accounts = self._collect_accounts(entries, options_map)

        def format_positions(inventory: Inventory) -> str:
            positions: Iterable = inventory.get_positions()
            items = [str(position) for position in positions]
            if not items:
                return "0"
            return ", ".join(sorted(items))

        lines: list[str] = []
        for account_name in sorted(accounts):
            node = realization.get_or_create(root_account, account_name)
            balance_str = format_positions(node.balance)
            lines.append(f"{account_name}: {balance_str}")

        error_strings = [str(err) for err in errors]
        return lines, error_strings

    def list_accounts(self, user_id: str) -> list[str]:
        ledger_path = self.user_ledger_path(user_id)
        if not ledger_path.exists() or ledger_path.stat().st_size == 0:
            return []

        entries, _, options_map = loader.load_file(str(ledger_path))
        accounts = self._collect_accounts(entries, options_map)
        return sorted(accounts)

    def posting_exists(
        self,
        user_id: str,
        account_name: str,
        amount: Union[Decimal, float, int, str],
        currency: str | None = None,
        *,
        date_str: str | None = None,
    ) -> bool:
        """Return True if a posting with the same date and amount already exists.

        This checks for duplicate transactions by date and amount only,
        ignoring description and counter-account differences.
        """

        ledger_path = self.user_ledger_path(user_id)
        if not ledger_path.exists() or ledger_path.stat().st_size == 0:
            return False

        entries, _, _ = loader.load_file(str(ledger_path))

        target_amount = self._to_decimal(amount)
        target_date: date | None = None
        if date_str:
            try:
                target_date = datetime.datetime.fromisoformat(date_str).date()
            except ValueError:
                target_date = None

        for entry in entries:
            postings = getattr(entry, "postings", None)
            if not postings:
                continue

            # Skip if date doesn't match
            if target_date and getattr(entry, "date", None) != target_date:
                continue

            # Check if any posting matches the account and amount
            for posting in postings:
                units: Amount | None = getattr(posting, "units", None)
                if units is None:
                    continue

                qty = self._to_decimal(units.number)

                if posting.account == account_name:
                    if currency and units.currency != currency:
                        continue
                    # Match if amount is equal (considering both positive and negative)
                    if qty == target_amount or qty == -target_amount:
                        return True

        return False

    @staticmethod
    def _normalize_entry(entry: str) -> str:
        lines = [line.rstrip() for line in entry.strip().splitlines()]
        return "\n".join(lines)

    @staticmethod
    def _ensure_trailing_newline(content: str) -> str:
        return content if content.endswith("\n") else content + "\n"

    @staticmethod
    def _to_decimal(value: Union[Decimal, float, int, str]) -> Decimal:
        if isinstance(value, Decimal):
            return value
        if isinstance(value, (int, float)):
            return Decimal(str(value))
        return Decimal(value)

    def _compose_content(self, existing_content: str, new_entries: list[str]) -> str:
        if not new_entries:
            if not existing_content:
                return ""
            normalized_existing = existing_content.rstrip("\n")
            return self._ensure_trailing_newline(normalized_existing)

        existing_normalized = existing_content.rstrip()
        new_entries_text = "\n\n".join(entry.rstrip() for entry in new_entries)
        if existing_normalized:
            combined = existing_normalized + "\n\n" + new_entries_text
        else:
            combined = new_entries_text

        return self._ensure_trailing_newline(combined.rstrip())

    def _collect_accounts(self, entries, options_map) -> set[str]:
        accounts: set[str] = set()
        if bquery is not None:
            try:
                result, _, _ = bquery.run_query(entries, options_map, "SELECT DISTINCT account FROM postings")
                accounts.update(row[0] for row in result if row and row[0])
            except Exception:
                accounts.clear()
        if not accounts:
            accounts.update(self._collect_accounts_manual(entries))
        return accounts

    @staticmethod
    def _collect_accounts_manual(entries) -> set[str]:
        accounts: set[str] = set()
        for entry in entries:
            account = getattr(entry, "account", None)
            if account:
                accounts.add(account)
            postings = getattr(entry, "postings", None)
            if postings:
                for posting in postings:
                    if posting.account:
                        accounts.add(posting.account)
        return accounts
