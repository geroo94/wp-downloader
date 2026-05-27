# WP Downloader 1.0 — Dokumentacja Techniczna

## 1. Wstęp
WP Downloader to nowoczesna aplikacja desktopowa służąca do pobierania materiałów wideo oraz audio z popularnych serwisów internetowych. Aplikacja łączy w sobie wydajność silnika `yt-dlp` z przyjaznym interfejsem użytkownika opartym na technologiach webowych.

## 2. Architektura Systemu
Projekt został zaprojektowany w architekturze hybrydowej:

*   **Frontend:** Interfejs zbudowany w HTML/JS/CSS, wyświetlany za pomocą komponentu `QWebEngineView` (Chromium).
*   **Backend:** Serwer API oparty na **FastAPI**, działający lokalnie.
*   **Integracja:** Komunikacja między warstwą Python a interfejsem odbywa się poprzez:
    *   **REST API:** Inicjowanie zadań, pobieranie list formatów.
    *   **WebSockets:** Powiadomienia w czasie rzeczywistym o postępie pobierania.
    *   **QWebChannel:** Natywne wywołania systemowych okien dialogowych (wybór folderu).

### Zarządzanie wątkami
Aplikacja wykorzystuje model wielowątkowy:
1.  **Główny Wątek (GUI):** Obsługa okna PyQt6 i zdarzeń systemowych.
2.  **Wątek Serwera (ServerThread):** Uruchamia pętlę `asyncio` dla serwera Uvicorn.
3.  **Workerzy (YtDlpWorker):** Wykonują procesy pobierania w tle, nie blokując interfejsu.

## 3. Struktura Plików

- `main.py`: Punkt wejścia aplikacji.
- `app_controller.py`: Koordynator łączący logikę biznesową z interfejsem.
- `main_window.py`: Definicja natywnego okna aplikacji i integracja z zasobnikiem systemowym (Tray).
- `server.py`: Definicje punktów końcowych API (FastAPI) i obsługa WebSocketów.
- `download_manager.py`: Zarządzanie kolejką zadań i stanem pobierań.
- `yt_dlp_worker.py`: Logika operacyjna wykorzystująca bibliotekę `yt-dlp`.

## 4. Wymagania i Instalacja

### Wymagania systemowe
- Python 3.11 lub nowszy.
- Narzędzie **FFmpeg** zainstalowane w systemie i dodane do zmiennej PATH (wymagane do łączenia strumieni wideo i audio).

### Instalacja bibliotek
```bash
pip install -r requirements.txt
```

## 5. Funkcje Aplikacji

- **Wielozadaniowość:** Możliwość dodawania wielu filmów do kolejki.
- **Wybór Jakości:** Automatyczne rozpoznawanie dostępnych formatów (1080p, 720p, 480p) oraz tryb "Tylko Audio".
- **Minimalizacja do Zasobnika:** Aplikacja może działać w tle, powiadamiając o zakończeniu zadań.
- **Dynamiczne Foldery:** Możliwość ustawienia globalnego folderu zapisu lub wybór osobnej lokalizacji dla konkretnego zadania.
- **Nagrywanie Streamów:** Możliwość pobierania streamów na żywo, z opcją oglądania pliku w trakcie pobierania (format MPEG-TS).

## 6. API Endpoints

| Metoda | Endpoint | Opis |
| :--- | :--- | :--- |
| `GET` | `/api/tasks` | Pobiera listę wszystkich zadań. |
| `POST` | `/api/download` | Dodaje nowe zadanie do kolejki. |
| `GET` | `/api/formats` | Pobiera listę dostępnych formatów dla danego URL. |
| `WS` | `/ws` | Strumień aktualizacji statusów w czasie rzeczywistym. |

## 7. Rozwiązywanie problemów

1.  **Błąd WebSocket:** Jeśli pasek postu się nie aktualizuje, należy odświeżyć okno (F5).
2.  **Brak formatów:** Upewnij się, że `yt-dlp` jest aktualny (`pip install -U yt-dlp`).
3.  **Błędy uprawnień:** Upewnij się, że aplikacja ma uprawnienia do zapisu w wybranym folderze.

---
*Dokumentacja wygenerowana automatycznie dla WP Downloader v1.0.*