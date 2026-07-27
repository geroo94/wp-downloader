"""WP Environment Checker — lekkie, niezależne narzędzie diagnostyczne.

"Krok 0" przed instalacją głównej aplikacji WP Downloader: skanuje komputer
pod kątem wymagań (system, VC++ Redistributable, GPU/sterowniki, prawa
zapisu, binarki pomocnicze, uprawnienia administratora) i pokazuje czytelną
listę kontrolną z przyciskiem "Kopiuj raport dla Administratora IT" — dla
środowisk korporacyjnych, gdzie user zgłasza brakujące biblioteki do IT
zamiast zgadywać co jest nie tak.

CELOWO zero-dependency poza stdlibem (Tkinter zamiast PyQt6): to narzędzie
ma działać PRZED zainstalowaniem/zaufaniem głównej aplikacji, więc nie może
zależeć od żadnego z jej modułów (server.py, binaries.py, ...) ani od
ciężkich pakietów (PyQt6, torch) — stąd też brak importu czegokolwiek z
reszty repo. Uruchamiane samodzielnie: `python tools/env_checker.py`, albo
jako spakowany `WP_Environment_Checker.exe` / `.app` (patrz build_env_checker.sh).
"""

from __future__ import annotations

import ctypes
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import threading
import tkinter as tk
from dataclasses import dataclass
from tkinter import font as tkfont
from tkinter import ttk

APP_TITLE = "WP Environment Checker"

# Poziomy wyniku pojedynczego sprawdzenia.
OK, WARN, FAIL = "ok", "warn", "fail"
_ICON = {OK: "✅", WARN: "⚠️", FAIL: "❌"}
_COLOR = {OK: "#2e9e5b", WARN: "#c98a1a", FAIL: "#d64545"}


@dataclass
class CheckResult:
    name: str
    level: str  # OK | WARN | FAIL
    detail: str


def _bundled_bin_dir() -> str:
    """Katalog `bin/` obok tego skryptu/EXE — ten sam layout co główna apka,
    ale sprawdzany BEZ importu binaries.py (patrz docstring modułu)."""
    if getattr(sys, "frozen", False):
        base = os.path.dirname(os.path.abspath(sys.executable))
    else:
        base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, "bin")


def _exe(name: str) -> str:
    # Mirror celowy, nie przeoczenie: to samo co binaries._exe_name() w
    # głównej apce. Ten plik świadomie nie importuje NIC z reszty repo (patrz
    # docstring modułu) — narzędzie musi dać się uruchomić nawet gdy główna
    # instalacja jest niekompletna/uszkodzona, więc drobna duplikacja stałej
    # jest tańsza niż zależność od stanu drugiego pakietu.
    return name + (".exe" if sys.platform == "win32" else "")


def check_os_version() -> CheckResult:
    system = platform.system()
    if system == "Darwin":
        ver = platform.mac_ver()[0] or "?"
        try:
            major = int(ver.split(".")[0])
        except (ValueError, IndexError):
            major = 0
        if major >= 12:
            return CheckResult("System operacyjny", OK, f"macOS {ver}")
        return CheckResult("System operacyjny", FAIL,
                            f"macOS {ver} — wymagane macOS 12 (Monterey) lub nowszy")
    if system == "Windows":
        ver = platform.win32_ver()[0] or "?"
        is_64 = platform.machine().endswith("64")
        release_ok = ver in ("10", "11") or ver == "post2012Server"
        arch_note = "x64" if is_64 else "x86 (NIEWSPIERANE — wymagane x64)"
        if release_ok and is_64:
            return CheckResult("System operacyjny", OK, f"Windows {ver} {arch_note}")
        return CheckResult("System operacyjny", FAIL,
                            f"Windows {ver} {arch_note} — wymagane Windows 10/11 x64")
    if system == "Linux":
        return CheckResult("System operacyjny", WARN,
                            f"Linux ({platform.platform()}) — wsparcie nieoficjalne")
    return CheckResult("System operacyjny", WARN, f"Nierozpoznany system: {system}")


def check_vcredist() -> CheckResult:
    """Microsoft Visual C++ Redistributable — wymagany na Windows przez
    torch/whisper/ffmpeg (msvcp140.dll, vcruntime140.dll). Nie dotyczy
    macOS/Linux — tam OK bez sprawdzania."""
    if sys.platform != "win32":
        return CheckResult("VC++ Redistributable", OK, "Nie dotyczy (macOS/Linux)")
    try:
        import winreg  # type: ignore[import-not-found]
        for hive, key in (
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\X64"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\VisualStudio\14.0\VC\Runtimes\X86"),
        ):
            try:
                with winreg.OpenKey(hive, key) as k:
                    installed, _ = winreg.QueryValueEx(k, "Installed")
                    version, _ = winreg.QueryValueEx(k, "Version")
                    if installed:
                        return CheckResult("VC++ Redistributable", OK, f"Zainstalowany ({version})")
            except OSError:
                continue
        # Fallback: obecność samych DLL-i w System32 (starsze/nietypowe instalki).
        sys32 = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "System32")
        if os.path.isfile(os.path.join(sys32, "msvcp140.dll")):
            return CheckResult("VC++ Redistributable", OK, "Wykryto msvcp140.dll w System32")
        return CheckResult("VC++ Redistributable", FAIL,
                            "Nie znaleziono — pobierz z aka.ms/vs/17/release/vc_redist.x64.exe")
    except Exception as exc:
        return CheckResult("VC++ Redistributable", WARN, f"Nie udało się sprawdzić: {exc}")


def check_gpu() -> CheckResult:
    """NVIDIA CUDA (Windows/Linux) lub Apple Silicon MPS (macOS). Brak GPU
    NIE jest błędem blokującym — Whisper ma bezpieczny fallback na CPU
    (patrz whisper_device.py w głównej aplikacji), więc WARN, nie FAIL."""
    if sys.platform == "darwin":
        if platform.machine() == "arm64":
            return CheckResult("GPU / akceleracja", OK, "Apple Silicon — Metal/MPS dostępne")
        return CheckResult("GPU / akceleracja", WARN,
                            "Mac Intel — brak MPS, transkrypcja na CPU")
    nvidia_smi = shutil.which("nvidia-smi")
    if nvidia_smi:
        try:
            r = subprocess.run(
                [nvidia_smi, "--query-gpu=name,driver_version", "--format=csv,noheader"],
                capture_output=True, text=True, timeout=5,
                # CREATE_NO_WINDOW — ten sam magic number co binaries.
                # subprocess_flags() w głównej apce; zduplikowany celowo,
                # patrz komentarz przy _exe() wyżej (brak importu z repo).
                creationflags=(0x08000000 if sys.platform == "win32" else 0),
            )
            line = (r.stdout or "").strip().splitlines()[0] if r.stdout.strip() else ""
            if r.returncode == 0 and line:
                return CheckResult("GPU / akceleracja", OK, f"NVIDIA: {line}")
        except Exception:
            pass
        return CheckResult("GPU / akceleracja", WARN,
                            "nvidia-smi obecny, ale nie zwrócił danych — sterownik może wymagać restartu")
    return CheckResult("GPU / akceleracja", WARN,
                        "Brak wykrytej karty NVIDIA — transkrypcja Whisper będzie działać na CPU")


def _writable_dirs() -> dict[str, str]:
    home = os.path.expanduser("~")
    if sys.platform == "darwin":
        return {
            "Downloads": os.path.join(home, "Downloads"),
            "Desktop": os.path.join(home, "Desktop"),
            "Application Support": os.path.join(home, "Library", "Application Support"),
        }
    if sys.platform == "win32":
        return {
            "Downloads": os.path.join(home, "Downloads"),
            "Desktop": os.path.join(home, "Desktop"),
            "AppData": os.environ.get("APPDATA", os.path.join(home, "AppData", "Roaming")),
        }
    return {
        "Downloads": os.path.join(home, "Downloads"),
        "Desktop": os.path.join(home, "Desktop"),
        "Dane aplikacji": os.path.join(home, ".local", "share"),
    }


def check_write_permissions() -> CheckResult:
    dirs = _writable_dirs()
    failed, missing = [], []
    for label, path in dirs.items():
        if not os.path.isdir(path):
            missing.append(label)
            continue
        try:
            fd, tmp_path = tempfile.mkstemp(prefix=".wp_env_check_", dir=path)
            os.close(fd)
            os.remove(tmp_path)
        except OSError:
            failed.append(label)
    if failed:
        return CheckResult("Prawa zapisu", FAIL, f"Brak zapisu w: {', '.join(failed)}")
    if missing:
        return CheckResult("Prawa zapisu", WARN, f"Katalogi nie istnieją: {', '.join(missing)}")
    return CheckResult("Prawa zapisu", OK, f"OK: {', '.join(dirs.keys())}")


def check_helper_binaries() -> CheckResult:
    """ffmpeg/ffprobe/deno — sprawdzane obok checkera (bin/, jeśli spakowany
    razem z główną apką) i w PATH systemowym, żeby wynik był sensowny także
    gdy Environment Checker jest uruchomiony samodzielnie, bez reszty apki."""
    bin_dir = _bundled_bin_dir()
    found, missing = [], []
    for name in ("ffmpeg", "ffprobe", "deno"):
        local = os.path.join(bin_dir, _exe(name))
        if os.path.isfile(local) or shutil.which(name):
            found.append(name)
        else:
            missing.append(name)
    if missing:
        return CheckResult(
            "Binarki pomocnicze (ffmpeg/ffprobe/deno)", WARN,
            f"Brak: {', '.join(missing)} — główna instalka WP Downloader dociąga je sama"
        )
    return CheckResult("Binarki pomocnicze (ffmpeg/ffprobe/deno)", OK,
                        f"Znalezione: {', '.join(found)}")


def check_admin_rights() -> CheckResult:
    if sys.platform == "win32":
        try:
            is_admin = bool(ctypes.windll.shell32.IsUserAnAdmin())  # type: ignore[attr-defined]
        except Exception:
            return CheckResult("Uprawnienia administratora", WARN, "Nie udało się sprawdzić")
        return CheckResult(
            "Uprawnienia administratora", OK if is_admin else WARN,
            "Konto administratora" if is_admin
            else "Konto standardowe — instalacja per-user zadziała, systemowa może wymagać IT"
        )
    if sys.platform == "darwin":
        is_root = os.geteuid() == 0
        return CheckResult("Uprawnienia administratora", OK,
                            "Uruchomiono jako root" if is_root
                            else "Konto standardowe — wystarczające (.app nie wymaga admina na macOS)")
    return CheckResult("Uprawnienia administratora", OK, "Nie dotyczy")


ALL_CHECKS = (
    check_os_version,
    check_vcredist,
    check_gpu,
    check_write_permissions,
    check_helper_binaries,
    check_admin_rights,
)


def run_all_checks() -> list[CheckResult]:
    results = []
    for fn in ALL_CHECKS:
        try:
            results.append(fn())
        except Exception as exc:
            results.append(CheckResult(fn.__name__, WARN, f"Błąd sprawdzenia: {exc}"))
    return results


def build_report_text(results: list[CheckResult]) -> str:
    lines = [
        f"{APP_TITLE} — raport środowiska",
        f"System: {platform.platform()}",
        f"Python: {platform.python_version()}",
        "-" * 48,
    ]
    for r in results:
        lines.append(f"[{r.level.upper():4}] {r.name}: {r.detail}")
    n_fail = sum(1 for r in results if r.level == FAIL)
    n_warn = sum(1 for r in results if r.level == WARN)
    lines.append("-" * 48)
    lines.append(f"Podsumowanie: {n_fail} błąd(y) krytyczne, {n_warn} ostrzeżenie(a)")
    return "\n".join(lines)


class EnvCheckerApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.results: list[CheckResult] = []
        root.title(APP_TITLE)
        root.geometry("620x520")
        root.minsize(520, 420)

        header_font = tkfont.Font(family="Helvetica", size=15, weight="bold")
        mono_font = tkfont.Font(family="Courier", size=10)

        header = ttk.Frame(root, padding=(16, 16, 16, 8))
        header.pack(fill="x")
        ttk.Label(header, text=APP_TITLE, font=header_font).pack(anchor="w")
        ttk.Label(
            header,
            text="Weryfikacja wymagań systemowych przed instalacją WP Downloader (Krok 0)",
        ).pack(anchor="w")

        self.list_frame = ttk.Frame(root, padding=(16, 4))
        self.list_frame.pack(fill="both", expand=True)

        self.status_var = tk.StringVar(value="Sprawdzanie środowiska…")
        ttk.Label(root, textvariable=self.status_var, padding=(16, 4)).pack(anchor="w")

        btns = ttk.Frame(root, padding=16)
        btns.pack(fill="x")
        self.copy_btn = ttk.Button(
            btns, text="Kopiuj raport dla Administratora IT",
            command=self._copy_report, state="disabled")
        self.copy_btn.pack(side="left")
        ttk.Button(btns, text="Sprawdź ponownie", command=self._rerun).pack(side="left", padx=8)
        ttk.Button(btns, text="Zamknij", command=root.destroy).pack(side="right")

        self._mono_font = mono_font
        self._rerun()

    def _rerun(self) -> None:
        for w in self.list_frame.winfo_children():
            w.destroy()
        self.copy_btn.configure(state="disabled")
        self.status_var.set("Sprawdzanie środowiska…")
        threading.Thread(target=self._run_in_background, daemon=True).start()

    def _run_in_background(self) -> None:
        results = run_all_checks()
        self.root.after(0, self._on_results, results)

    def _on_results(self, results: list[CheckResult]) -> None:
        self.results = results
        for r in results:
            row = ttk.Frame(self.list_frame)
            row.pack(fill="x", pady=3)
            icon = ttk.Label(row, text=_ICON[r.level], width=3)
            icon.pack(side="left")
            text = ttk.Label(row, text=f"{r.name}: {r.detail}", wraplength=520, justify="left")
            text.pack(side="left", fill="x", expand=True)
        n_fail = sum(1 for r in results if r.level == FAIL)
        n_warn = sum(1 for r in results if r.level == WARN)
        if n_fail:
            self.status_var.set(f"⚠ {n_fail} błąd(y) krytyczne — instalacja może się nie powieść")
        elif n_warn:
            self.status_var.set(f"{n_warn} ostrzeżenie(a) — środowisko sprawne, kilka uwag")
        else:
            self.status_var.set("Wszystko sprawne — środowisko gotowe na WP Downloader")
        self.copy_btn.configure(state="normal")

    def _copy_report(self) -> None:
        report = build_report_text(self.results)
        self.root.clipboard_clear()
        self.root.clipboard_append(report)
        # clipboard_append wymaga update, żeby zawartość przetrwała zamknięcie okna
        # zanim schowek zdąży ją realnie przejąć od procesu Tk.
        self.root.update()
        self.status_var.set("Raport skopiowany do schowka ✓")


def main() -> None:
    root = tk.Tk()
    EnvCheckerApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
