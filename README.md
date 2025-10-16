# Beancount Telegram Bot

Beancount Telegram Bot lets you capture bookkeeping data straight from chat. It uses an LLM to understand natural-language messages, turns them into structured Beancount transactions, and writes them into the bundled ledger files. A built-in Fava web UI makes it easy to browse accounts and reports, and the deployment supports multiple users and multiple ledgers in parallel.

## Highlights
- LLM-powered parsing converts everyday instructions into balanced Beancount entries
- Ledger storage is handled in `data/beancount`, so your transactions persist across restarts
- Fava dashboard ships with the container for quick access to balances, charts, and reports
- Multi-user, multi-ledger support keeps each Telegram user’s books separate in one deployment

## Quick Start
1. Rename the sample environment file:
   ```bash
   mv .env.example .env
   ```
2. Rename the sample Compose file:
   ```bash
   mv compose.yml.example compose.yml
   ```
3. Edit `.env` and `compose.yml` to add your Telegram bot token, LLM API credentials, storage paths, and any other required secrets. Once saved, follow your usual Docker or Docker Compose workflow to build the image and launch the container.
   ```bash
   docker compose up -d
   ```

After these preparations, build and run the stack with Docker/Docker Compose as you normally would.
