# WP Downloader

Aplikacja desktopowa (macOS/Windows) do pobierania i obróbki wideo: pobieranie z yt-dlp, Live Stream DVR, Fast Cutter z brandingiem WP, oraz transkrypcja Whisper AI z akceleracją sprzętową.

## 🚀 Przegląd i główne funkcje

- **Pobieranie wideo** — silnik yt-dlp in-process (bez zależności od systemowego Pythona), automatyczny merge audio/wideo, historia pobrań z podglądem postępu na żywo.
- **Live Stream DVR** — podgląd streamów na żywo z buforem cofania (timeshift), pozwala nagrać dowolny fragment trwającej transmisji.
- **Fast Cutter** — precyzyjne cięcie wideo z automatycznym brandingiem WP: logo, tekst źródła, animacja SUB oraz outro nakładane jako osobna warstwa (nie zastępuje końcówki materiału — wydłuża całość, patrz sekcja *Delay/Overlap* w aplikacji).
- **Transkrypcja Whisper AI** — lokalna transkrypcja audio/wideo na tekst (.txt/.srt) w wielu językach, z wyborem trybu obliczeń CPU/GPU (patrz niżej).
- **Zero-dependency** — ffmpeg, ffprobe, yt-dlp i deno są bundlowane wewnątrz aplikacji; nie wymaga instalacji Homebrew/systemowych narzędzi.

---

## 🛠️ Krok 0: Weryfikacja środowiska (WP Environment Checker)

Przed instalacją głównej aplikacji, szczególnie na komputerach firmowych z ograniczonymi uprawnieniami, **zalecamy uruchomienie WP Environment Checker** — małego, niezależnego narzędzia diagnostycznego (kilka MB, bez instalacji).

Sprawdza automatycznie:

| Kontrola | Co weryfikuje |
|---|---|
| Wersja systemu | Windows 10/11 x64 lub macOS 12+ |
| VC++ Redistributable | Wymagany na Windows przez silnik Whisper/ffmpeg |
| GPU / akceleracja | NVIDIA CUDA (Windows/Linux) lub Apple Silicon MPS (macOS) |
| Prawa zapisu | Downloads, Pulpit, AppData/Application Support |
| Binarki pomocnicze | ffmpeg, ffprobe, deno |
| Uprawnienia administratora | Konto standardowe vs administrator |

Wynik to czytelna checklista (✅/⚠️/❌) z przyciskiem **„Kopiuj raport dla Administratora IT”** — jednym kliknięciem generuje tekstowy raport do wklejenia w zgłoszeniu do działu IT, jeśli czegoś brakuje.

**Pobierz:**
- Windows: `WP_Environment_Checker.exe`
- macOS: `WP_Environment_Checker_macOS.zip`

---

## 📦 Instrukcja instalacji krok po kroku

### Windows

**Wersja instalacyjna (zalecana):**
1. Pobierz `WP_Downloader_Setup.exe`.
2. Uruchom instalator — instaluje się w profilu użytkownika (`%LOCALAPPDATA%`), **bez uprawnień administratora**.
3. Windows SmartScreen może pokazać ostrzeżenie („Windows ochronił Twój komputer") — kliknij **Więcej informacji → Uruchom mimo to** (aplikacja nie ma jeszcze podpisu EV, jest to oczekiwane).

**Wersja przenośna (portable):**
1. Pobierz `WP_Downloader_Windows.zip`.
2. Rozpakuj do dowolnego folderu (np. pendrive, folder bez uprawnień admina).
3. Uruchom `WP_Downloader.exe` ze środka rozpakowanego folderu.

### macOS

**Wersja .dmg (zalecana):**
1. Pobierz `WP_Downloader_macOS.dmg` i otwórz.
2. Przeciągnij `WP Downloader.app` do folderu `Applications`.
3. Przy pierwszym uruchomieniu Gatekeeper zablokuje aplikację jako pochodzącą od niezidentyfikowanego dewelopera. Odblokuj jednym z dwóch sposobów:
   - **GUI:** kliknij prawym na `WP Downloader.app` → **Otwórz** → potwierdź w oknie dialogowym (tylko raz, przy pierwszym uruchomieniu).
   - **Terminal:** usuń atrybut kwarantanny ręcznie:
     ```bash
     xattr -cr "/Applications/WP Downloader.app"
     ```

**Wersja portable (bez instalacji do /Applications):**
1. Pobierz `WP_Downloader_macOS_PORTABLE.zip` i rozpakuj.
2. Odblokuj Gatekeepera tym samym poleceniem, wskazując rozpakowaną ścieżkę:
   ```bash
   xattr -cr "/ścieżka/do/WP Downloader.app"
   ```
3. Uruchom aplikację z rozpakowanego folderu.

---

## 🧠 Wymagania sprzętowe i akceleracja GPU

Transkrypcja Whisper AI działa zarówno na CPU, jak i z akceleracją GPU. Tryb wybiera się w zakładce **Transkrypcja** → **Tryb obliczeń Whisper**:

| Tryb | Opis | Kiedy używać |
|---|---|---|
| **Automatyczny** (domyślny) | Wykrywa i używa najlepszego dostępnego sprzętu (CUDA → MPS → CPU) | Zalecany dla większości użytkowników |
| **Wymuś GPU (CUDA/MPS)** | Wymusza akcelerację sprzętową; bezpieczny fallback na CPU, jeśli GPU niedostępne | Gdy auto-detekcja zawodzi mimo posiadanej karty |
| **Wymuś CPU** | Zawsze CPU, niezależnie od dostępnego GPU | Diagnostyka, niestabilne sterowniki GPU |

| Platforma | Akceleracja | Wymagania |
|---|---|---|
| Windows / Linux | NVIDIA CUDA | Karta NVIDIA + aktualny sterownik (`nvidia-smi` musi działać) |
| macOS (Apple Silicon: M1/M2/M3/M4/M5) | Metal (MPS) | Wbudowane, brak dodatkowej konfiguracji |
| macOS (Intel) / brak GPU | CPU (wielowątkowe) | Zawsze dostępne, wolniejsze przy dużych modelach |

**Modele Whisper** (zakładka Transkrypcja → Model): `tiny`/`base` — szybkie, niższa jakość; `small` — zalecany balans; `medium`/`large-v3` — najwyższa jakość, wymaga więcej RAM/VRAM i czasu.

---

## 🔧 Rozwiązywanie problemów (Troubleshooting)

**"Windows chronił Twój komputer" (SmartScreen)**
Aplikacja nie ma jeszcze certyfikatu EV code-signing. Kliknij **Więcej informacji → Uruchom mimo to**. To nie jest błąd — dotyczy każdej niepodpisanej aplikacji na Windows.

**macOS: „Nie można otworzyć, ponieważ pochodzi od niezidentyfikowanego dewelopera"**
Uruchom `xattr -cr "/Applications/WP Downloader.app"` w Terminalu (patrz sekcja instalacji wyżej), albo kliknij prawym → Otwórz.

**Brak VC++ Redistributable (Windows)**
Whisper/ffmpeg wymagają Microsoft Visual C++ Redistributable. Pobierz i zainstaluj: [aka.ms/vs/17/release/vc_redist.x64.exe](https://aka.ms/vs/17/release/vc_redist.x64.exe) (wymaga uprawnień administratora — poproś dział IT o instalację, jeśli konto jest ograniczone).

**Środowiska korporacyjne z ograniczeniami administratora**
- Instalator Windows domyślnie instaluje się w `%LOCALAPPDATA%` — **nie wymaga uprawnień administratora**.
- Jeśli polityka firmowa blokuje uruchamianie plików `.exe` z pobranych źródeł, użyj **WP Environment Checker** (patrz Krok 0) i skopiuj wygenerowany raport do zgłoszenia w dziale IT — zawiera dokładną listę brakujących komponentów.
- Brak sterownika GPU nie blokuje działania aplikacji — transkrypcja automatycznie przełącza się na CPU.

**Transkrypcja nie wykrywa GPU mimo posiadanej karty NVIDIA**
Sprawdź w terminalu/wierszu poleceń, czy `nvidia-smi` w ogóle działa — jeśli zwraca błąd, problem leży w sterowniku NVIDIA, nie w aplikacji. Zaktualizuj sterownik i uruchom ponownie WP Downloader.

**Aplikacja nie startuje / crashuje przy starcie**
Uruchom **WP Environment Checker** — sprawdzi w kilka sekund, czy brakuje VC++ Redistributable, praw zapisu, lub czy binarki pomocnicze (ffmpeg/ffprobe) są dostępne.

---

## 📥 Pliki do pobrania w tym wydaniu

| Plik | Platforma | Opis |
|---|---|---|
| `WP_Downloader_Setup.exe` | Windows | Instalator (zalecany) |
| `WP_Downloader_Windows.zip` | Windows | Wersja przenośna |
| `WP_Downloader_macOS.dmg` | macOS | Instalator drag & drop |
| `WP_Downloader_macOS_PORTABLE.zip` | macOS | Wersja przenośna |
| `WP_Environment_Checker.exe` | Windows | Krok 0 — diagnostyka środowiska |
| `WP_Environment_Checker_macOS.zip` | macOS | Krok 0 — diagnostyka środowiska |
