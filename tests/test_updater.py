"""Testy logiki auto-aktualizacji z GitHub Releases.

Skupione na CZYSTEJ logice (porównanie wersji, wybór assetu dla platformy),
bo to ona decyduje, czy user dostanie „jesteś aktualny" czy pobierze 700 MB
niepotrzebnie — albo, gorzej, paczkę dla złego systemu.

Uruchomienie: python3 tests/test_updater.py
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from updater import (  # noqa: E402
    APP_VERSION,
    compare_versions,
    parse_version,
    pick_asset_for_platform,
    is_update_available,
)

failed = 0


def check(desc: str, got, expected) -> None:
    global failed
    if got == expected:
        print(f"  OK   {desc}")
    else:
        failed += 1
        print(f"  FAIL {desc}\n       oczekiwano: {expected!r}\n       otrzymano:  {got!r}")


print("=== parse_version: tolerancja na prefiks 'v' i śmieci ===")
# GitHub w tym repo ma OBA tagi: `1.0` i `v1.0` — releases/latest zwraca `1.0`.
check("goły tag", parse_version("1.0"), (1, 0, 0))
check("z prefiksem v", parse_version("v1.0"), (1, 0, 0))
check("trzyczłonowa", parse_version("v1.2.3"), (1, 2, 3))
check("z białymi znakami", parse_version("  v2.10.4 "), (2, 10, 4))
check("jednoczłonowa", parse_version("v3"), (3, 0, 0))
check("sufiks pre-release ignorowany", parse_version("v1.2.3-beta1"), (1, 2, 3))
check("śmieci → zera", parse_version("nonsens"), (0, 0, 0))
check("pusty", parse_version(""), (0, 0, 0))

print("\n=== compare_versions ===")
check("równe mimo prefiksu", compare_versions("1.0", "v1.0"), 0)
check("nowsza minor", compare_versions("1.0", "1.1"), -1)
check("starsza minor", compare_versions("1.1", "1.0"), 1)
check("10 > 9 (nie leksykalnie!)", compare_versions("1.9", "1.10"), -1)
check("patch", compare_versions("1.0.1", "1.0.2"), -1)
check("major", compare_versions("2.0", "1.99"), 1)
check("1.0 == 1.0.0", compare_versions("1.0", "1.0.0"), 0)

print("\n=== is_update_available ===")
check("ten sam tag", is_update_available("1.0", "1.0"), False)
check("ten sam tag z v", is_update_available("1.0", "v1.0"), False)
check("nowszy zdalny", is_update_available("1.0", "v1.0.1"), True)
check("starszy zdalny (rollback NIE jest update'em)",
      is_update_available("1.2", "v1.0"), False)
check("brak zdalnego tagu", is_update_available("1.0", ""), False)

print("\n=== pick_asset_for_platform ===")
ASSETS = [
    {"name": "WP_Downloader_macOS.dmg", "browser_download_url": "u1", "size": 1},
    {"name": "WP_Downloader_macOS_PORTABLE.zip", "browser_download_url": "u2", "size": 2},
    {"name": "WP_Downloader_Setup.exe", "browser_download_url": "u3", "size": 3},
    {"name": "WP_Downloader_Windows.zip", "browser_download_url": "u4", "size": 4},
    {"name": "WP_Environment_Checker.exe", "browser_download_url": "u5", "size": 5},
    {"name": "WP_Environment_Checker_macOS.zip", "browser_download_url": "u6", "size": 6},
]

mac = pick_asset_for_platform(ASSETS, "darwin")
check("macOS bierze .dmg głównej apki", mac and mac["name"], "WP_Downloader_macOS.dmg")

win = pick_asset_for_platform(ASSETS, "win32")
check("Windows bierze instalator .exe", win and win["name"], "WP_Downloader_Setup.exe")

# REGRESJA: Environment Checker to OSOBNE narzędzie — nigdy nie może zostać
# wzięte jako aktualizacja głównej aplikacji (oba mają .exe/.zip w nazwie!).
no_main = [a for a in ASSETS if a["name"].startswith("WP_Environment_Checker")]
check("sam EnvChecker → brak kandydata (nie podmieniamy apki checkerem)",
      pick_asset_for_platform(no_main, "win32"), None)
check("sam EnvChecker (mac) → brak kandydata",
      pick_asset_for_platform(no_main, "darwin"), None)

# Portable jako fallback gdy brak instalatora/dmg
only_portable_win = [a for a in ASSETS if a["name"] == "WP_Downloader_Windows.zip"]
check("Windows fallback na portable zip",
      pick_asset_for_platform(only_portable_win, "win32")["name"],
      "WP_Downloader_Windows.zip")
only_portable_mac = [a for a in ASSETS if a["name"] == "WP_Downloader_macOS_PORTABLE.zip"]
check("macOS fallback na portable zip",
      pick_asset_for_platform(only_portable_mac, "darwin")["name"],
      "WP_Downloader_macOS_PORTABLE.zip")

check("pusta lista", pick_asset_for_platform([], "darwin"), None)
check("nieznana platforma", pick_asset_for_platform(ASSETS, "linux"), None)

print("\n=== APP_VERSION sanity ===")
check("APP_VERSION parsuje się", parse_version(APP_VERSION) != (0, 0, 0), True)

print(f"\n{'ALL PASS' if not failed else str(failed) + ' FAILED'}")
sys.exit(1 if failed else 0)
