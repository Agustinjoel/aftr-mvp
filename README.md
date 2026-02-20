# AFTR MVP

## Windows (PowerShell): fix `No module named pytest`

Ese error aparece cuando corrés `python -m pytest ...` con el Python global de Windows
(en lugar del Python de `.venv`).

### Opción recomendada (1 comando)

```powershell
.\scripts\run_stats_tests.ps1
```

Este script:
1. Crea `.venv` si no existe.
2. Instala dependencias de `requirements.txt` dentro de `.venv`.
3. Corre `tests/test_stats_summary.py` con `\.venv\Scripts\python.exe`.

### Opción manual

```powershell
cd C:\Users\amastrocola\Desktop\engine
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pytest -q tests/test_stats_summary.py
```

> Evitá usar `python -m pytest ...` sin prefijar `\.venv\Scripts\python.exe` en esa PC.

## Daily run (refresh + tests + app)

Para hacer todo en cadena (instalar deps, refrescar data, correr tests y levantar app):

```powershell
.\scripts\run_daily.ps1
```

Opciones útiles:

```powershell
# Saltar refresh
.\scripts\run_daily.ps1 -SkipRefresh

# Saltar tests
.\scripts\run_daily.ps1 -SkipTests

# Cambiar puerto
.\scripts\run_daily.ps1 -Port 8010
```

## Run app local

```powershell
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Abrí:
- http://127.0.0.1:8000
- http://127.0.0.1:8000/docs