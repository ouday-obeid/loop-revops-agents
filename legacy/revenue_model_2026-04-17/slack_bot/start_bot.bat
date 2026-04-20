@echo off
:: Restart loop — if the bot crashes, wait 10 seconds and restart
:loop
cd /d "C:\Users\odayo\revenue_model\slack_bot"
python app.py
echo Bot exited. Restarting in 10 seconds...
timeout /t 10 /nobreak >nul
goto loop
