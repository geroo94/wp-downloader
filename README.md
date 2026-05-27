# WP Downloader v1.0

Desktopowa aplikacja do pobierania wideo i audio z YouTube, Vimeo i setek innych serwisów. Działa jako samodzielna aplikacja — bez konieczności instalowania Pythona.

---

## Funkcje

- **Pobieranie wideo i audio** z YouTube, Vimeo, Twitch i setek innych serwisów (silnik yt-dlp)
- **Wybór formatu i jakości** — MP4 (1080p / 720p / 480p), WebM, MP3, M4A lub dowolny format z listy
- **Nagrywanie live streamów** — pobieranie transmisji na żywo od samego początku (`--live-from-start`)
  - Format H.264 + M4A: nativnie obsługiwany przez QuickTime (macOS) i Windows Media Player
  - Przycisk „Zatrzymaj i zapisz" — natychmiastowe zatrzymanie + automatyczne scalanie pliku MP4
  - Podgląd postępu fragmentów (`V: frag 150/1200 | A: frag 150/1200`)
- **Kolejka wielozadaniowa** — kilka pobierań jednocześnie, historia zadań
- **Automatyczne scalanie** — FFmpeg łączy strumienie video i audio po zatrzymaniu
- **Minimalizacja do zasobnika systemowego** — działa w tle
- **Aktualizacje yt-dlp** — wbudowany mechanizm aktualizacji silnika

---

## Instalacja — użytkownik końcowy

### macOS

#### 1. Pobierz aplikację

Pobierz plik `WP_Downloader.app` z zakładki [Releases](https://github.com/geroo94/wp-downloader/releases) i przenieś go do folderu `/Aplikacje`.

#### 2. Zainstaluj FFmpeg

FFmpeg jest wymagany do scalania strumieni wideo i audio (szczególnie przy live streamach).

**Opcja A — przez Homebrew (zalecane):**

Jeśli nie masz Homebrew, zainstaluj go najpierw — wklej w Terminal:
```bash
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
```

Następnie zainstaluj FFmpeg:
```bash
brew install ffmpeg
```

**Opcja B — ręcznie:**

Pobierz gotowy plik binarny ze strony [ffmpeg.org/download.html](https://ffmpeg.org/download.html), rozpakuj i skopiuj `ffmpeg` do `/usr/local/bin/` lub `/opt/homebrew/bin/`.

#### 3. Uruchom aplikację

Przy pierwszym uruchomieniu macOS zablokuje aplikację (Gatekeeper). Aby otworzyć:
- Kliknij prawym przyciskiem myszy na `WP_Downloader.app`
- Wybierz **„Otwórz"**
- W oknie dialogowym kliknij **„Otwórz mimo to"**

---

### Windows

#### 1. Pobierz aplikację

Pobierz plik `WP_Downloader_Windows.zip` z zakładki [Releases](https://github.com/geroo94/wp-downloader/releases), rozpakuj i uruchom `WP_Downloader\WP_Downloader.exe` — nie wymaga instalacji.

> **Uwaga Windows Defender:** Przy pierwszym uruchomieniu może pojawić się ostrzeżenie SmartScreen. Kliknij **„Więcej informacji"** → **„Uruchom mimo to"**.

#### 2. Zainstaluj FFmpeg

FFmpeg jest wymagany do scalania strumieni wideo i audio.

**Opcja A — przez winget (zalecane, Windows 10/11):**

Otwórz **PowerShell** lub **Terminal** jako administrator i wpisz:
```powershell
winget install --id Gyan.FFmpeg -e
```

**Opcja B — przez Chocolatey:**
```powershell
choco install ffmpeg
```

**Opcja C — ręcznie:**

1. Wejdź na [ffmpeg.org/download.html](https://ffmpeg.org/download.html) → sekcja Windows → pobierz wersję „full build"
2. Wypakuj archiwum ZIP, np. do `C:\ffmpeg\`
3. Dodaj `C:\ffmpeg\bin\` do zmiennej środowiskowej `PATH`:
   - Otwórz **Panel sterowania** → **System** → **Zaawansowane ustawienia systemu** → **Zmienne środowiskowe**
   - W sekcji „Zmienne systemowe" znajdź `Path`, kliknij **Edytuj**
   - Dodaj nowy wpis: `C:\ffmpeg\bin`
   - Zatwierdź i uruchom ponownie komputer

#### 3. Uruchom aplikację

Uruchom `WP_Downloader.exe` — aplikacja otworzy się z interfejsem webowym w wbudowanej przeglądarce.

---

## Instalacja — tryb deweloperski

Wymagania: **Python 3.11+** oraz **FFmpeg** (instalacja jak powyżej dla danego systemu).

### macOS

```bash
# 1. Sklonuj repozytorium
git clone https://github.com/geroo94/wp-downloader.git
cd wp-downloader

# 2. Utwórz środowisko wirtualne
python3 -m venv venv
source venv/bin/activate

# 3. Zainstaluj zależności
pip install -r requirements.txt

# 4. Uruchom
python main.py
```

### Windows

```powershell
# 1. Sklonuj repozytorium
git clone https://github.com/geroo94/wp-downloader.git
cd wp-downloader

# 2. Utwórz środowisko wirtualne
python -m venv venv
venv\Scripts\activate

# 3. Zainstaluj zależności
pip install -r requirements.txt

# 4. Uruchom
python main.py
```

---

## Budowanie aplikacji (.app / .exe)

```bash
# Upewnij się, że masz zainstalowany PyInstaller
pip install pyinstaller

# Zbuduj aplikację
python build_app.py
```

Gotowa aplikacja pojawi się w folderze `dist/`:
- macOS: `dist/WP_Downloader.app`
- Windows: `dist/WP_Downloader/` (folder z `WP_Downloader.exe` w środku)

---

## Obsługa live streamów

1. Przejdź do zakładki **Live Streamy**
2. Wklej URL transmisji (YouTube, Twitch itp.)
3. Opcjonalnie: kliknij **Sprawdź formaty** i wybierz jakość
4. Podaj ścieżkę zapisu i kliknij **Nagraj stream**
5. Aby zakończyć: kliknij **Zatrzymaj i zapisz**
   - Aplikacja natychmiast zatrzymuje pobieranie
   - Automatycznie scala video + audio przez FFmpeg
   - Wynikowy plik MP4 jest gotowy do odtworzenia

> Aplikacja domyślnie wybiera H.264 + M4A — format kompatybilny z QuickTime (macOS), Windows Media Player i VLC bez dodatkowych kodeków.

---

## Gdzie trafiają pobrane pliki?

Domyślnie:
```
macOS:   ~/Downloads/WP Downloader/
Windows: C:\Users\<login>\Downloads\WP Downloader\
```

Możesz zmienić folder zapisu bezpośrednio w interfejsie aplikacji.

---

## Architektura

```
WP Downloader
├── main.py              ← punkt wejścia, inicjalizacja PyQt6
├── app_controller.py    ← koordynator (GUI ↔ serwer)
├── main_window.py       ← okno Qt, zasobnik systemowy (tray)
├── server_thread.py     ← wątek z pętlą asyncio + Uvicorn
├── server.py            ← API REST i WebSocket (FastAPI)
├── download_manager.py  ← kolejka zadań, stan pobierań
├── yt_dlp_worker.py     ← silnik pobierania (yt-dlp Python API)
├── environment_manager.py ← wykrywanie FFmpeg, yt-dlp, Python
├── queue_manager.py     ← zarządzanie kolejką
├── static/
│   └── index.html       ← interfejs użytkownika (HTML/CSS/JS)
├── requirements.txt
├── build_app.py         ← skrypt budowania PyInstaller
└── wp_downloader.spec   ← konfiguracja PyInstaller
```

**Stos technologiczny:**
- **PyQt6** — natywne okno i zasobnik systemowy
- **QWebEngineView** — interfejs webowy (Chromium)
- **FastAPI + Uvicorn** — lokalny serwer HTTP + WebSocket
- **yt-dlp Python API** — pobieranie (bez subprocess — ważne dla EXE)
- **FFmpeg** — scalanie strumieni, konwersja formatów

---

## API Endpoints

| Metoda | Endpoint | Opis |
|--------|----------|------|
| `GET` | `/api/tasks` | Lista wszystkich zadań |
| `POST` | `/api/download` | Dodaj zadanie pobierania |
| `GET` | `/api/formats?url=...` | Dostępne formaty dla URL |
| `POST` | `/api/tasks/{id}/cancel` | Anuluj aktywne pobieranie |
| `POST` | `/api/tasks/{id}/stop` | Graceful stop dla live streamu |
| `DELETE` | `/api/tasks/{id}` | Usuń zakończone zadanie |
| `POST` | `/api/tasks/reorder` | Zmień kolejność zadań |
| `GET` | `/api/system-info` | Wersje yt-dlp, Python, FFmpeg |
| `POST` | `/api/system/update` | Aktualizuj yt-dlp |
| `WS` | `/ws` | WebSocket — aktualizacje na żywo |

---

## Rozwiązywanie problemów

**FFmpeg nie znaleziony (macOS)**
```bash
brew install ffmpeg
```

**FFmpeg nie znaleziony (Windows)**
```powershell
winget install --id Gyan.FFmpeg -e
```
Następnie uruchom ponownie aplikację.

**„Requested format is not available"**
→ Kliknij „Sprawdź formaty" i wybierz dostępny format z listy

**Live stream: brak dźwięku w nagranym pliku**
→ Upewnij się że używasz domyślnego formatu (H.264 + M4A) — nie VP9

**Aplikacja nie otwiera się (macOS Gatekeeper)**
→ Kliknij prawym przyciskiem → Otwórz → Otwórz mimo to

**Aplikacja nie otwiera się (Windows SmartScreen)**
→ Kliknij „Więcej informacji" → „Uruchom mimo to"

**Pasek postępu się nie aktualizuje**
→ Odśwież widok (F5) — WebSocket powinien się połączyć ponownie

**Błąd przy uruchamianiu w trybie deweloperskim**
```bash
python main.py
# Sprawdź log: wp_downloader_debug.log (obok pliku EXE lub skryptu)
```

---

## Zależności (requirements.txt)

| Biblioteka | Zastosowanie |
|------------|-------------|
| `yt-dlp` | Silnik pobierania |
| `fastapi` | Serwer API |
| `uvicorn` | ASGI server |
| `PyQt6` | GUI (natywne okno) |
| `PyQt6-WebEngine` | Wbudowana przeglądarka |
| `websockets` | WebSocket |

---

## Licencja

MIT License — szczegóły w pliku `LICENSE`.
