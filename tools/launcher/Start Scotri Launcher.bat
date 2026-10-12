@echo off
REM Launches the Scotri Service Launcher GUI with no console window.
start "" "C:\Scotri\services\.venv\Scripts\pythonw.exe" "%~dp0scotri_launcher.py"
