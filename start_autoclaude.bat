@echo off
rem Runs autoclaude in watch mode: works through the Notion queue,
rem sleeps through token resets, and picks up new projects automatically.
cd /d "%~dp0"
python autoclaude.py --watch %*
pause
