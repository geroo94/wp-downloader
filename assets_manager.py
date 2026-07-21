"""assets_manager.py — trwałe zarządzanie zasobami Fast Cuttera (Outro/Sub).

Persystencja: config.json w Application Support/WP_Downloader/ (macOS) lub
odpowiedniku Windows/Linux (ten sam katalog bazowy co main.py.setup_overlay_
site_packages / server.get_overlay_dir — jeden wspólny "app data" root).

Rozróżnienie "Wybierz wideo…" vs "Dodaj nowe wideo…":
  - Wybierz = jednorazowe wskazanie pliku z dysku, żadnej kopii ani wpisu
    w rejestrze — frontend po prostu wysyła surową ścieżkę do renderu.
  - Dodaj = plik jest KOPIOWANY do custom_assets/<kind>/ i dopisywany do
    config.json na stałe — przetrwa restart aplikacji, nawet gdy user
    usunie/przeniesie oryginał. Widoczny w dropdownie jako kolejna opcja.

Reset WYŁĄCZNIE przez clear_all_custom_assets() — wołane z
/api/system/clear-cache, nigdy automatycznie.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import time
import uuid

logger = logging.getLogger(__name__)

_VALID_KINDS = ("outro", "sub")


def get_app_data_dir() -> str:
    """Wspólny root danych usera — ten sam wzorzec co main.py/server.py."""
    import platform
    home = os.path.expanduser("~")
    sysname = platform.system()
    if sysname == "Darwin":
        base = os.path.join(home, "Library", "Application Support", "WP_Downloader")
    elif sysname == "Windows":
        base = os.path.join(os.environ.get("APPDATA", home), "WP_Downloader")
    else:
        base = os.path.join(home, ".local", "share", "wp_downloader")
    os.makedirs(base, exist_ok=True)
    return base


def _custom_assets_dir(kind: str) -> str:
    d = os.path.join(get_app_data_dir(), "custom_assets", kind)
    os.makedirs(d, exist_ok=True)
    return d


def _config_path() -> str:
    return os.path.join(get_app_data_dir(), "config.json")


def _load_config() -> dict:
    p = _config_path()
    if os.path.isfile(p):
        try:
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and isinstance(data.get("custom_assets"), dict):
                return data
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("assets_manager: config.json nieczytelny (%s) — reset do pustego", exc)
    return {"custom_assets": {"outro": [], "sub": []}}


def _save_config(cfg: dict) -> None:
    p = _config_path()
    tmp = p + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        os.replace(tmp, p)
    except OSError as exc:
        logger.error("assets_manager: nie można zapisać config.json: %s", exc)


def list_custom_assets(kind: str) -> list[dict]:
    """Zwraca [{id, name, path, added_at}] — samoczynnie usuwa z rejestru
    wpisy, których plik zniknął z dysku (np. user ręcznie skasował z Findera)."""
    if kind not in _VALID_KINDS:
        return []
    cfg = _load_config()
    items = cfg.get("custom_assets", {}).get(kind, [])
    valid = [it for it in items if os.path.isfile(it.get("path", ""))]
    if len(valid) != len(items):
        cfg["custom_assets"][kind] = valid
        _save_config(cfg)
    return valid


def add_custom_asset(kind: str, source_path: str) -> dict:
    """Kopiuje source_path do custom_assets/<kind>/ i dopisuje trwały wpis
    do config.json. Zwraca {id, name, path, added_at} nowego wpisu."""
    if kind not in _VALID_KINDS:
        raise ValueError(f"Nieznany rodzaj zasobu: {kind}")
    if not os.path.isfile(source_path):
        raise FileNotFoundError(source_path)

    dest_dir = _custom_assets_dir(kind)
    base_name = os.path.basename(source_path)
    asset_id = uuid.uuid4().hex[:8]
    _, ext = os.path.splitext(base_name)
    dest_path = os.path.join(dest_dir, f"{asset_id}{ext}")
    shutil.copy2(source_path, dest_path)

    entry = {"id": asset_id, "name": base_name, "path": dest_path, "added_at": time.time()}
    cfg = _load_config()
    cfg.setdefault("custom_assets", {}).setdefault(kind, []).append(entry)
    _save_config(cfg)
    logger.info("assets_manager: dodano %s custom asset '%s' -> %s", kind, base_name, dest_path)
    return entry


def clear_all_custom_assets() -> int:
    """Wywoływane WYŁĄCZNIE z 'Wyczyść pamięć podręczną' w Ustawieniach —
    kasuje cały katalog custom_assets/ i resetuje config.json do stanu
    fabrycznego (puste listy). Zwraca liczbę usuniętych plików."""
    base = os.path.join(get_app_data_dir(), "custom_assets")
    count = 0
    if os.path.isdir(base):
        for _root, _dirs, files in os.walk(base):
            count += len(files)
        shutil.rmtree(base, ignore_errors=True)
    cfg = _load_config()
    cfg["custom_assets"] = {"outro": [], "sub": []}
    _save_config(cfg)
    return count
