@echo off
REM ============================================================
REM  English SpeakApp - lanceur
REM  Double-cliquez sur ce fichier pour demarrer l'application.
REM  Il lance le serveur puis ouvre Chrome sur l'app.
REM ============================================================

REM Se placer dans le dossier de ce script (peu importe d'ou on l'appelle)
cd /d "%~dp0"

echo Demarrage du serveur English SpeakApp...

REM Lancer le serveur dans une fenetre separee (qui reste ouverte)
start "English SpeakApp - serveur" venv\Scripts\python.exe -m uvicorn backend.server:app --host 127.0.0.1 --port 8000

REM Laisser ~2 secondes au serveur pour demarrer
REM (ping plutot que timeout : fonctionne meme sans clavier disponible)
ping -n 3 127.0.0.1 >nul

REM Ouvrir l'app dans Chrome (avec repli sur le navigateur par defaut)
set "CHROME=C:\Program Files\Google\Chrome\Application\chrome.exe"
if not exist "%CHROME%" set "CHROME=C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"

if exist "%CHROME%" (
    start "" "%CHROME%" "http://127.0.0.1:8000/"
) else (
    start "" "http://127.0.0.1:8000/"
)

echo.
echo L'app est lancee : http://127.0.0.1:8000/
echo Pour arreter le serveur, fermez la fenetre "English SpeakApp - serveur".
echo.
exit
