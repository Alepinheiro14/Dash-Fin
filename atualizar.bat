@echo off
cd /d "%~dp0"

for /f %%d in ('powershell -NoProfile -Command "Get-Date -Format yyyy-MM-dd"') do set HOJE=%%d

echo [%HOJE%] Iniciando coleta de curvas de juros...
".venv\Scripts\python.exe" coletor_curvas_py.py
if errorlevel 1 (
    echo [ERRO] coletor_curvas falhou.
)

echo [%HOJE%] Iniciando coleta de mercado...
".venv\Scripts\python.exe" coletor_mercado.py
if errorlevel 1 (
    echo [ERRO] coletor_mercado falhou.
)

git add historico_curvas.csv historico_mercado.csv
git diff --staged --quiet && (
    echo [git] Sem mudancas. Nenhum commit necessario.
) || (
    git commit -m "dados: atualizar historicos - %HOJE%"
    echo [git] Commit realizado para %HOJE%.
)
