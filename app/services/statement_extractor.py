from __future__ import annotations

import base64
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import List

from openai import OpenAI
from pydantic import BaseModel, Field

from ..config import get_settings
from .beancount_service import BeancountService


class StatementPeriod(BaseModel):
    start_date: str = Field(description="YYYY-MM-DD")
    end_date: str = Field(description="YYYY-MM-DD")


class Transaction(BaseModel):
    date: str = Field(description="YYYY-MM-DD")
    description: str
    amount: float = Field(description="Signed transaction amount in statement currency")
    debit: str = Field(description="Name of the debited account")
    credit: str = Field(description="Name of the credited account")


class BankStatement(BaseModel):
    institution: str
    account_holder: str
    account_number: str
    currency: str
    ledger_account: str = Field(
        description="Name of the beancount ledger account this statement should be posted to"
    )
    statement_period: StatementPeriod
    opening_balance: float
    closing_balance: float
    transactions: List[Transaction]


PROMPT_TEMPLATE = (
    "You are a financial extraction engine. "
    "Extract statement information from the provided document, determine which ledger account the statement belongs to, "
    "and classify each transaction. Always return valid JSON that matches the supplied schema. "
    "For every transaction emit exactly these keys and nothing else: date, description, amount, debit, credit."
    " amount must be signed relative to the statement ledger: positive amounts indicate money entering the ledger account, negative amounts indicate money leaving it."
    " When amount < 0, set `debit` to the ledger account itself and `credit` to the counterparty account (e.g. an expense)."
    " When amount > 0, set `credit` to the ledger account itself and `debit` to the counterparty account (e.g. income or transfers)."
    " Reverse the chronological order from the statement: output entries so the newest transaction appears first and the oldest last."
    " If the statement or transaction text contains Chinese characters or merchant names, prioritize picking ledger/counterparty accounts whose currency is CNY or whose names mention Chinese institutions."
    " If any transaction date is missing a year, assume the year is {reference_year} and keep the given month/day."
    " YOU MAY ONLY USE ACCOUNT NAMES FROM THE ALLOWED LIST BELOW.\n\n"
    "Strict example format: [{{\"date\":\"2024-05-02\",\"description\":\"Sample\",\"amount\":-12.34,\"debit\":\"Assets:Bank:Kaspi:Gold\",\"credit\":\"Expenses:Food\"}}]\n"
    "Allowed account names (verbatim only):\n{allowed_accounts}\n\n"
    "User account summary:\n{account_summary}\n\n"
    "Recent transaction history (reuse the same ledger/counter accounts when descriptions are similar; most recent first):\n{history_block}\n\n"
    "{user_note_block}"
)


class StatementExtractor:
    IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg"}

    def __init__(self, model: str | None = None) -> None:
        settings = get_settings()
        api_key = settings.openai_api_key
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY is not configured")
        self.client = OpenAI(api_key=api_key)
        self.model = model or settings.openai_model or "gpt-4.1"
        self.beancount = BeancountService.from_settings()

    def extract(self, user_id: str, statement_path: Path, user_note: str | None = None) -> BankStatement:
        account_summary, allowed_accounts, history_lines = self._get_account_context(user_id)
        reference_year = str(datetime.now().year)
        prompt = self._build_prompt(account_summary, allowed_accounts, history_lines, reference_year, user_note)

        response = self.client.responses.parse(
            model=self.model,
            input=[{"role": "user", "content": self._build_input_content(statement_path, prompt)}],
            temperature=0,
            top_p=0.1,
            text_format=BankStatement,
        )

        for output in response.output:
            if output.type != "message":
                continue
            for item in output.content:
                if getattr(item, "type", None) == "output_text" and getattr(item, "parsed", None):
                    statement: BankStatement = item.parsed
                    statement.transactions.reverse()
                    self._validate_statement(statement, allowed_accounts)
                    return statement

        raise RuntimeError("Parsed response is missing structured content")

    def generate_entries(
        self,
        statement: BankStatement,
        user_id: str,
        source: Path,
    ) -> tuple[list[str], int, int]:
        new_entries: list[str] = []
        skipped = 0
        ledger_account = statement.ledger_account.strip()
        history_records = self.beancount.history_records(user_id)
        for txn in statement.transactions:
            ledger_change = Decimal(str(txn.amount))
            if ledger_change == 0:
                skipped += 1
                continue

            counter_account = self._resolve_counter_account(ledger_account, txn)

            suggested_counter = self.beancount.suggest_counter_account(
                user_id,
                txn.description,
                ledger_account,
                history=history_records,
            )
            if suggested_counter and suggested_counter != counter_account and suggested_counter != ledger_account:
                counter_account = suggested_counter

            # Check for duplicates by date and amount only
            if self.beancount.posting_exists(
                user_id,
                ledger_account,
                ledger_change,
                statement.currency,
                date_str=txn.date,
            ):
                skipped += 1
                continue

            new_entries.append(
                self._render_entry(
                    statement,
                    txn,
                    counter_account=counter_account,
                )
            )

        if not new_entries:
            return [], 0, skipped

        heading = self._render_heading_comment(source)
        return [heading, *new_entries], len(new_entries), skipped

    def _get_account_context(self, user_id: str) -> tuple[str, list[str], list[str]]:
        lines, errors = self.beancount.summarize_accounts(user_id)
        accounts = self.beancount.list_accounts(user_id)
        history_lines = self.beancount.transaction_history_summary(user_id)
        if not accounts:
            raise RuntimeError("No beancount accounts available; cannot classify transactions.")
        if not lines:
            lines = ["(no account balances available)"]
        summary = "\n".join(lines)
        if errors:
            summary += "\nWarnings: " + "; ".join(errors)
        return summary, accounts, history_lines

    def _build_prompt(
        self,
        account_summary: str,
        allowed_accounts: list[str],
        history_lines: list[str],
        reference_year: str,
        user_note: str | None,
    ) -> str:
        allowed_block = "\n".join(allowed_accounts)
        history_block = (
            "\n".join(history_lines)
            if history_lines
            else "No prior transactions found; use the allowed accounts consistently."
        )
        note_block = f"Additional user note: {user_note}" if user_note else ""
        return PROMPT_TEMPLATE.format(
            account_summary=account_summary,
            allowed_accounts=allowed_block,
            history_block=history_block,
            reference_year=reference_year,
            user_note_block=note_block,
        )

    def _build_input_content(self, statement_path: Path, prompt: str) -> list[dict[str, object]]:
        suffix = statement_path.suffix.lower()
        if suffix == ".pdf":
            with statement_path.open("rb") as handle:
                file_obj = self.client.files.create(file=handle, purpose="user_data")
            return [
                {"type": "input_file", "file_id": file_obj.id},
                {"type": "input_text", "text": prompt},
            ]

        if suffix in self.IMAGE_EXTENSIONS:
            mime = "image/png" if suffix == ".png" else "image/jpeg"
            data = base64.b64encode(statement_path.read_bytes()).decode("ascii")
            data_url = f"data:{mime};base64,{data}"
            return [
                {"type": "input_image", "image_url": data_url},
                {"type": "input_text", "text": prompt},
            ]

        raise RuntimeError(
            f"Unsupported statement file type '{statement_path.suffix}'. Use PDF or one of: {', '.join(sorted(self.IMAGE_EXTENSIONS))}."
        )

    def _validate_statement(self, statement: BankStatement, allowed_accounts: list[str]) -> None:
        allowed = set(acc.strip() for acc in allowed_accounts)
        missing: set[str] = set()
        ledger = statement.ledger_account.strip()
        if ledger not in allowed:
            missing.add(ledger)
        for txn in statement.transactions:
            debit = txn.debit.strip()
            credit = txn.credit.strip()
            amount = Decimal(str(txn.amount))
            if debit not in allowed:
                missing.add(debit)
            if credit not in allowed:
                missing.add(credit)
            if amount < 0 and debit != ledger:
                raise RuntimeError(
                    f"Transaction on {txn.date} should debit {ledger} because amount is negative, got {debit}"
                )
            if amount > 0 and credit != ledger:
                raise RuntimeError(
                    f"Transaction on {txn.date} should credit {ledger} because amount is positive, got {credit}"
                )
        if missing:
            raise RuntimeError(
                "Model produced account names not present in the ledger: " + ", ".join(sorted(missing))
            )

    def _render_entry(
        self,
        statement: BankStatement,
        txn: Transaction,
        *,
        counter_account: str | None = None,
    ) -> str:
        ledger_account = statement.ledger_account.strip()
        counter_account = counter_account or self._resolve_counter_account(ledger_account, txn)
        ledger_amount = Decimal(str(txn.amount))
        counter_amount = -ledger_amount
        description = self._sanitize_description(txn.description)
        return "\n".join(
            [
                f"{txn.date} * \"{description}\"",
                f"  {ledger_account}  {self._format_decimal(ledger_amount)} {statement.currency}",
                f"  {counter_account}  {self._format_decimal(counter_amount)} {statement.currency}",
            ]
        )

    @staticmethod
    def _resolve_counter_account(ledger_account: str, txn: Transaction) -> str:
        ledger = ledger_account.strip()
        debit_hint = txn.debit.strip()
        credit_hint = txn.credit.strip()
        amount = Decimal(str(txn.amount))
        if amount < 0:
            counter = credit_hint if credit_hint and credit_hint != ledger else debit_hint
        else:
            counter = debit_hint if debit_hint and debit_hint != ledger else credit_hint
        if not counter or counter == ledger:
            raise RuntimeError("Model did not supply a valid counter account distinct from the ledger account.")
        return counter

    @staticmethod
    def _format_decimal(value: Decimal) -> str:
        text = format(value, "f")
        if "." in text:
            text = text.rstrip("0").rstrip(".")
        return text or "0"

    @staticmethod
    def _sanitize_description(description: str) -> str:
        cleaned = description.replace("\n", " ").strip()
        return cleaned.replace('"', "'")

    @staticmethod
    def _render_heading_comment(source: Path) -> str:
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        return f"; =========== import {source.name} at {timestamp} ==========="
