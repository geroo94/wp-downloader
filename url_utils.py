"""url_utils.py — sanityzacja adresów URL wklejanych przez użytkownika.

Powód powstania (realny bug, log Windows 2026-08-27):
    https://www.youtube.com/watch?v=TAPYNkViPZIhttps://www.youtube.com/watch?v=TAPYNkViPZI
User wkleił link dwa razy (Ctrl+V ×2 w to samo pole). Ani frontend
(`.value.trim()`), ani backend (`url.startswith(("http://","https://"))`)
tego nie wyłapały — sklejony string przeszedł walidację i poleciał do yt-dlp.

Dla YouTube akurat zadziałało mimo wszystko (regex ekstraktora yt-dlp łapie
11-znakowe ID i się zatrzymuje — zweryfikowane empirycznie), ale to
przypadek: dla innych serwisów i innych kształtów URL sklejony link po
prostu nie zadziała. Sanityzujemy więc na wejściu, w jednym miejscu,
współdzielonym przez `server.py` i `yt_dlp_worker.py`.

Frontend ma bliźniaczą implementację w `static/index.html`
(funkcja `sanitizeUrl`) — pola trzymamy zsynchronizowane, bo user powinien
WIDZIEĆ oczyszczony link w polu, zanim kliknie Pobierz.
"""

from __future__ import annotations

import re

# Parametry czysto śledzące/analityczne — usuwane, bo nie wpływają na to CO
# pobieramy, a potrafią psuć dopasowanie ekstraktorów i zaśmiecać logi.
# Świadomie usuwamy po NAZWACH (blacklist), nie zostawiamy tylko wybranych
# (whitelist) — whitelist wycięłaby parametry istotne dla serwisów, których
# dziś nie znamy (Vimeo `h=`, prywatne tokeny itp.).
_TRACKING_PARAMS = frozenset({
    "ab_channel",      # YouTube: nazwa kanału doklejana przez "Kopiuj link"
    "si",              # YouTube/Spotify: share identifier
    "pp",              # YouTube: parametr sesji odtwarzacza
    "feature",         # YouTube: youtu.be / share / emb_title
    "fbclid",          # Facebook click id
    "gclid",           # Google Ads click id
    "igshid",          # Instagram share id
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
})

# Pierwszy schemat http(s) w tekście.
_URL_START_RE = re.compile(r"https?://", re.IGNORECASE)

# Znaki, po których kolejne `https://` jest CZĘŚCIĄ pierwszego adresu, a nie
# doklejonym drugim linkiem — nie wolno tam ciąć:
#   `=`  → wartość parametru:  ...?next=https://cel.pl/film
#   `,` `|` → separator listy w parametrze
#   `/`  → URL zagnieżdżony w ścieżce: https://proxy/https://cel.pl/v.mp4
# Bez tej straży sanityzacja NISZCZYŁA poprawne linki (zweryfikowane:
# `?next=https://other.com/video` robiło się `?next=`).
_EMBEDDED_BEFORE = frozenset("=,|/")


def sanitize_url(raw: str) -> str:
    """Zwraca pojedynczy, czysty URL wyciągnięty z tego, co wkleił user.

    1. Przycina białe znaki.
    2. Wyciąga PIERWSZY adres http(s) i ucina wszystko od miejsca, w którym
       zaczyna się kolejny `http://`/`https://` — to naprawia sklejone
       wklejenia (`...ID` + `https://...` bez separatora). Zwykły regex
       `https?://[^\\s]+` tu NIE wystarcza, bo jest zachłanny i połknąłby
       oba adresy naraz (brak spacji między nimi).
    3. Usuwa parametry śledzące, zachowując kolejność i wszystkie parametry
       istotne (`v`, `list`, `t`, `index`, ...).

    Gdy w tekście nie ma żadnego `http(s)://`, zwraca przycięty input bez
    zmian — walidacja w `server.py` odrzuci go z czytelnym komunikatem,
    zamiast żeby ta funkcja po cichu produkowała pusty string.
    """
    if not raw:
        return ""
    text = raw.strip()

    first = _URL_START_RE.search(text)
    if not first:
        return text

    url = text[first.start():]

    # Białe znaki (i wszystko po nich) nie należą już do adresu.
    url = url.split()[0] if url.split() else url

    url = _cut_repeated_paste(url)
    return _strip_tracking_params(url)


def _cut_repeated_paste(url: str) -> str:
    """Ucina drugi (i kolejny) adres doklejony przez wielokrotne wklejenie.

    Rozstrzygamy DWOMA niezależnymi sygnałami, bo sam „drugi https:// = tnij"
    niszczył poprawne linki z zagnieżdżonym URL-em:

    1. **Dokładna powtórka** — reszta zaczyna się dokładnie tym samym tekstem
       co początek. To jednoznaczny podpis podwójnego Ctrl+V i tniemy zawsze,
       nawet po `/` czy `=`. Dzięki temu działa też link kończący się ukośnikiem
       (`https://vimeo.com/kanal/` ×2), którego sama heurystyka znaku by nie
       złapała.
    2. **Heurystyka znaku** — dla sklejeń, które nie są dokładną powtórką
       (dwa różne linki wklejone pod rząd). Tniemy tylko wtedy, gdy przed
       schematem NIE stoi znak z `_EMBEDDED_BEFORE`, czyli gdy to na pewno
       nie jest URL osadzony w parametrze/ścieżce.
    """
    start = _URL_START_RE.match(url)
    if not start:
        return url
    nxt = _URL_START_RE.search(url, start.end())
    if not nxt:
        return url

    head = url[:nxt.start()]
    # (1) dokładna powtórka — bezpieczne cięcie niezależnie od znaku przed
    if url[nxt.start():].startswith(head):
        return head
    # (2) sklejenie dwóch różnych linków — tnij tylko poza kontekstem osadzenia
    if url[nxt.start() - 1] not in _EMBEDDED_BEFORE:
        return head
    return url


def _strip_tracking_params(url: str) -> str:
    """Usuwa `_TRACKING_PARAMS` z query stringa, resztę zostawia BAJT W BAJT.

    Świadomie operujemy na surowym tekście (split po `&`), a NIE przez
    parse_qsl+urlencode ani przez `new URL()`. Powód: każde dekodowanie i
    ponowne kodowanie zmienia bajty, których nie wolno ruszać —
      * `p=a/b` stałoby się `p=a%2Fb`,
      * `+` vs `%20`, wielkość liter w hoście, `:443`, IDN → punycode.
    Dla linków z podpisem (bezpośrednie media, m3u8) podpis obejmuje dosłowne
    bajty query — przekodowanie unieważniłoby go. Dodatkowo bliźniak w JS
    używa tej samej metody, więc obie strony dają IDENTYCZNY wynik.
    """
    scheme_sep = url.find("?")
    if scheme_sep == -1:
        return url

    head, _, tail = url.partition("?")
    query, hash_sep, fragment = tail.partition("#")

    kept = [p for p in query.split("&")
            if p and p.split("=", 1)[0].lower() not in _TRACKING_PARAMS]
    if len(kept) == len([p for p in query.split("&") if p]):
        return url  # nic nie usunięto — oddajemy oryginał nietknięty

    new_query = "&".join(kept)
    out = head + (("?" + new_query) if new_query else "")
    if hash_sep:
        out += "#" + fragment
    return out
