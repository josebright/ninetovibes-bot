python -m src.orchestrator_v2 --generate-clips bamboo --count 30



# If you want to run one right now manually:
.venv/bin/python -m src.orchestrator_v2 --character bamboo --niche finance --fmt short --skip-script-review


# If the Telegram bot ever stops, restart it with:
cd /Users/uchennaugwumadu/Downloads/pipeline
nohup .venv/bin/python -m src.notify.telegram_bot --run > data/logs/telegram_bot.log 2>&1 &




# terminal 1 — bot must be polling for buttons to work
python3 -m src.notify.telegram_bot --run

# terminal 2 — fire the pipeline
python3 -m src.orchestrator_v2 --character bamboo --fmt short

