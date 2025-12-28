import textwrap
from datetime import date
from pathlib import Path

import pytest

from app.services.beancount_service import BeancountService


def _write_sample_ledger(tmp_path: Path, user_id: str) -> Path:
    content = textwrap.dedent(
        """
        option "operating_currency" "USD"
        2000-01-01 open Assets:Bank:Checking
        2000-01-01 open Assets:Cash
        2000-01-01 open Expenses:Food
        2000-01-01 open Income:Salary

        2024-01-10 * "Coffee Shop" "Latte"
          Assets:Bank:Checking -5 USD
          Expenses:Food 5 USD

        2024-01-12 * "Coffee Shop" "Snack"
          Assets:Bank:Checking -7 USD
          Expenses:Food 7 USD

        2024-01-15 * "ACME Corp" "Paycheck"
          Assets:Bank:Checking 1000 USD
          Income:Salary -1000 USD

        2024-01-20 * "Coffee Shop" "Cash payment"
          Assets:Cash -3 USD
          Expenses:Food 3 USD
        """
    ).strip()

    ledger_path = tmp_path / f"{user_id}.bean"
    ledger_path.write_text(content + "\n", encoding="utf-8")
    return ledger_path


@pytest.fixture()
def service_with_history(tmp_path: Path) -> BeancountService:
    user_id = "user"
    _write_sample_ledger(tmp_path, user_id)
    return BeancountService(root=tmp_path)


def test_history_records_capture_counts_and_dates(service_with_history: BeancountService):
    records = service_with_history.history_records("user")
    key = "coffee shop"
    assert key in records

    record = records[key]
    expected_pair = ("Assets:Bank:Checking", "Expenses:Food")
    assert record.pair_counts[expected_pair] == 2
    assert record.last_date == date(2024, 1, 20)


def test_suggest_counter_account_prefers_matching_ledger(service_with_history: BeancountService):
    records = service_with_history.history_records("user")

    bank_counter = service_with_history.suggest_counter_account(
        "user",
        "Coffee Shop latte",
        "Assets:Bank:Checking",
        history=records,
    )
    assert bank_counter == "Expenses:Food"

    cash_counter = service_with_history.suggest_counter_account(
        "user",
        "Coffee Shop latte",
        "Assets:Cash",
        history=records,
    )
    assert cash_counter == "Expenses:Food"


def test_transaction_history_summary_formats_recent_pairs(service_with_history: BeancountService):
    lines = service_with_history.transaction_history_summary("user", limit=3)
    assert any('"Coffee Shop"' in line for line in lines)
    assert any("Assets:Bank:Checking vs Expenses:Food" in line for line in lines)
    assert any("last 2024-01-20" in line for line in lines)
