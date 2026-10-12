@echo off
rem Start the widget with a console, so problems are visible.
setlocal
set "HERE=%~dp0"
"%HERE%.venv\Scripts\python.exe" "%HERE%app.py" --debug
