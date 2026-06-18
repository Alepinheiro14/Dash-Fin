@echo off
cd /d "%~dp0"

for /f %%d in ('powershell -NoProfile -Command "Get-Date -Format yyyy-MM-dd"') do set HOJE=%%d

echo [%HOJE%] Iniciando coleta de curvas de juros...
".venv\Scripts\python.exe" coletor_curvas_py.py
if errorlevel 1 (
    echo [ERRO] Coletor falhou. Abortando commit.
    exit /b 1
)

git add historico_curvas.csv
git diff --staged --quiet && (
    echo [git] Sem mudancas no historico. Nenhum commit necessario.
) || (
    git commit -m "dados: atualizar historico_curvas.csv - %HOJE%"
    echo [git] Commit realizado para %HOJE%.
)
