# Instalacja WP Downloader na macOS

## Standardowa procedura (rekomendowana)

1. Pobierz `WP_Downloader_macOS.dmg` z [Releases](https://github.com/geroo94/wp-downloader/releases/tag/v1.0).
2. Otwórz pobrany plik DMG (dwuklik). Zobaczysz okno z ikoną aplikacji **WP Downloader** obok skrótu **Applications**.
3. **Przeciągnij ikonę WP Downloader na ikonę Applications** (klasyczny wzorzec macOS).
4. Wyrzuć okno DMG (cmd+E) — instalacja zakończona.
5. **Pierwsze uruchomienie**: w Finderze otwórz folder Applications, **kliknij prawym** na WP Downloader → **Otwórz**. macOS pokaże ostrzeżenie „Nieznany deweloper / Apple nie może zweryfikować twórcy" → kliknij **Otwórz** w popup.
6. Każde kolejne uruchomienie: zwykły dwuklik na ikonę w Launchpad/Dock/Applications.

> **Dlaczego ostrzeżenie?** Nie mamy płatnego certyfikatu Apple Developer ($99/rok). Aplikacja jest podpisywana ad-hoc (lokalnie generowanym kluczem) — Gatekeeper pokaże ostrzeżenie tylko **raz**, przy pierwszym uruchomieniu. Po zaakceptowaniu macOS zapamiętuje twoją decyzję.

---

## Jeśli macOS uparcie blokuje („uszkodzony plik" / „cannot be opened")

W rzadkich przypadkach (zwłaszcza macOS 14+ na Apple Silicon) Gatekeeper umieszcza **quarantine attribute** na plikach z DMG i odmawia uruchomienia nawet po prawym→Otwórz. Wtedy wykonaj w **Terminalu** (otwórz przez Spotlight: cmd+Space → Terminal):

```bash
xattr -dr com.apple.quarantine /Applications/WP_Downloader.app
```

To usuwa atrybut quarantine. Po tym dwuklik powinien działać normalnie.

---

## Dezinstalacja

Wystarczy przeciągnąć `/Applications/WP_Downloader.app` do Kosza. Dane użytkownika (logi, preferencje):

- Logi: `~/Library/Logs/WP_Downloader/`
- Cache pobieranych yt-dlp/streamlink: `~/Library/Application Support/WP_Downloader/`
- WebView profil: `~/.wp_downloader/`

Można usunąć ręcznie.

---

## Diagnostyka problemów

- **Aplikacja nie startuje (crash przy otwarciu)** — sprawdź log: `~/Library/Logs/WP_Downloader/wp_downloader_*.log`
- **Pobieranie z YouTube zwraca 403** — sprawdź czy `dist/WP_Downloader.app/Contents/Resources/bin/deno` istnieje (deno JS runtime jest wymagany dla YouTube od 2026+; powinien być w bundlu)
- **macOS pokazuje „Apple nie może zweryfikować…" mimo prawym→Otwórz** — uruchom `xattr -dr com.apple.quarantine /Applications/WP_Downloader.app`
