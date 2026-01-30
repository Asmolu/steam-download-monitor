# Steam Download Monitor

Python-скрипт для фонового мониторинга загрузки игр в Steam:
- определяет Steam path (Windows: через реестр / fallback)
- определяет активную загрузку по `steamapps/downloading/<appid>`
- выводит скорость каждые 60 секунд (5 минут)
- пытается учитывать паузу (по дельте размера + эвристика по log)

## Requirements
- Python 3.10+ (на Windows обычно 3.11/3.12 ок)

## Run
```powershell
python .\steam_download_monitor.py