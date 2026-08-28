"""Testy sanityzacji URL — regresja dla buga ze zduplikowanym linkiem.

Reprodukuje realny przypadek z logu Windows (2026-08-27):
    https://www.youtube.com/watch?v=TAPYNkViPZIhttps://www.youtube.com/watch?v=TAPYNkViPZI
gdzie user wkleił link dwukrotnie, a ani frontend (.value.trim()) ani backend
(startswith("http")) tego nie wyłapały.

Uruchomienie: python3 tests/test_url_utils.py
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from url_utils import sanitize_url  # noqa: E402

CASES: list[tuple[str, str, str]] = [
    # (opis, wejście, oczekiwane wyjście)
    (
        "zduplikowany link z logu (bez separatora)",
        "https://www.youtube.com/watch?v=TAPYNkViPZIhttps://www.youtube.com/watch?v=TAPYNkViPZI",
        "https://www.youtube.com/watch?v=TAPYNkViPZI",
    ),
    (
        "zduplikowany link ze spacją",
        "https://youtu.be/abc123 https://youtu.be/abc123",
        "https://youtu.be/abc123",
    ),
    (
        "zduplikowany link http (nie https)",
        "http://example.com/vhttp://example.com/v",
        "http://example.com/v",
    ),
    (
        "poprawny link zostaje nietknięty",
        "https://www.youtube.com/watch?v=TAPYNkViPZI",
        "https://www.youtube.com/watch?v=TAPYNkViPZI",
    ),
    (
        "otaczające spacje/nowe linie",
        "  \n https://www.youtube.com/watch?v=TAPYNkViPZI \t ",
        "https://www.youtube.com/watch?v=TAPYNkViPZI",
    ),
    (
        "tekst przed linkiem — wyciągamy pierwszy URL",
        "zobacz to: https://youtu.be/abc123",
        "https://youtu.be/abc123",
    ),
    (
        "parametr śledzący ab_channel usunięty",
        "https://www.youtube.com/watch?v=TAPYNkViPZI&ab_channel=SomeChannel",
        "https://www.youtube.com/watch?v=TAPYNkViPZI",
    ),
    (
        "parametr si (youtu.be share) usunięty",
        "https://youtu.be/TAPYNkViPZI?si=Xy1234abcd",
        "https://youtu.be/TAPYNkViPZI",
    ),
    (
        "utm_* usunięte, v zachowane",
        "https://www.youtube.com/watch?v=TAPYNkViPZI&utm_source=news&utm_medium=mail",
        "https://www.youtube.com/watch?v=TAPYNkViPZI",
    ),
    # ── Parametry ISTOTNE muszą przetrwać (regresja: nie wolno ich zjeść) ──
    (
        "znacznik czasu t zachowany",
        "https://youtu.be/TAPYNkViPZI?t=42",
        "https://youtu.be/TAPYNkViPZI?t=42",
    ),
    (
        "playlist list zachowana, si usunięte",
        "https://www.youtube.com/watch?v=abc&list=PL123&si=track",
        "https://www.youtube.com/watch?v=abc&list=PL123",
    ),
    (
        "kolejność istotnych parametrów zachowana",
        "https://www.youtube.com/watch?v=abc&index=3&t=10",
        "https://www.youtube.com/watch?v=abc&index=3&t=10",
    ),
    # ── Przypadki brzegowe ──
    (
        "pusty string",
        "",
        "",
    ),
    (
        "brak http — zwracamy przycięty input (walidacja backendu odrzuci)",
        "to nie jest link",
        "to nie jest link",
    ),
    (
        "zduplikowany link Z parametrami śledzącymi naraz",
        "https://www.youtube.com/watch?v=ID1&ab_channel=Xhttps://www.youtube.com/watch?v=ID1&ab_channel=X",
        "https://www.youtube.com/watch?v=ID1",
    ),
    (
        "potrójne wklejenie",
        "https://youtu.be/abchttps://youtu.be/abchttps://youtu.be/abc",
        "https://youtu.be/abc",
    ),
    (
        "fragment #t= zachowany",
        "https://example.com/v#t=30",
        "https://example.com/v#t=30",
    ),
    # ── REGRESJA: URL zagnieżdżony w parametrze NIE może zostać ucięty ──
    # Pierwsza wersja sanityzacji cięła na KAŻDYM drugim `https://`, przez co
    # `?next=https://other.com/video` robiło się `?next=` — czyli naprawa
    # jednego buga psuła poprawne linki. Te przypadki to strażnik.
    (
        "URL w parametrze ?next= zachowany w całości",
        "https://example.com/play?next=https://other.com/video",
        "https://example.com/play?next=https://other.com/video",
    ),
    (
        "URL w parametrze ?url= (http) zachowany",
        "https://site.com/embed?url=http://cdn.com/a.m3u8",
        "https://site.com/embed?url=http://cdn.com/a.m3u8",
    ),
    (
        "URL zagnieżdżony w ścieżce (proxy) zachowany",
        "https://proxy.com/https://cel.com/v.mp4",
        "https://proxy.com/https://cel.com/v.mp4",
    ),
    (
        "URL zakodowany procentowo nietknięty",
        "https://player.com/?src=https%3A%2F%2Fcdn.com%2Fv.mp4",
        "https://player.com/?src=https%3A%2F%2Fcdn.com%2Fv.mp4",
    ),
    (
        "auth + port zachowane",
        "https://user:pass@host.com:8443/v.mp4",
        "https://user:pass@host.com:8443/v.mp4",
    ),
    # ── REGRESJA: link kończący się `/` wklejony dwukrotnie ──
    # Sama heurystyka „nie tnij po `/`" przepuszczała taki sklejony link.
    # Łapie go dopiero sygnał „dokładna powtórka".
    (
        "duplikat linku kończącego się ukośnikiem",
        "https://vimeo.com/channels/staffpicks/https://vimeo.com/channels/staffpicks/",
        "https://vimeo.com/channels/staffpicks/",
    ),
    (
        "duplikat twitch z ukośnikiem",
        "https://www.twitch.tv/videos/12345/https://www.twitch.tv/videos/12345/",
        "https://www.twitch.tv/videos/12345/",
    ),
    # ── REGRESJA: żadnej normalizacji URL-a (parytet z JS + podpisy) ──
    # parse_qsl+urlencode / new URL() zmieniały bajty: host na małe litery,
    # `/`→`%2F`, usuwanie `:443`. Dla linków z podpisem to je unieważnia.
    (
        "host i port NIE są normalizowane przy usuwaniu trackera",
        "https://EXAMPLE.com:443/A/b?si=1&v=2",
        "https://EXAMPLE.com:443/A/b?v=2",
    ),
    (
        "ukośnik w wartości parametru NIE jest przekodowany",
        "https://ex.com/v?si=1&p=a/b",
        "https://ex.com/v?p=a/b",
    ),
    (
        "pusty parametr zachowany przy usuwaniu trackera",
        "https://ex.com/v?si=1&flag",
        "https://ex.com/v?flag",
    ),
    (
        "usunięcie jedynego parametru nie zostawia gołego '?'",
        "https://ex.com/v?si=1",
        "https://ex.com/v",
    ),
    (
        "fragment zachowany gdy tracker usunięty z query",
        "https://ex.com/v?si=1&t=5#frag",
        "https://ex.com/v?t=5#frag",
    ),
]


def main() -> int:
    failed = 0
    for desc, raw, expected in CASES:
        got = sanitize_url(raw)
        if got == expected:
            print(f"  OK   {desc}")
        else:
            failed += 1
            print(f"  FAIL {desc}")
            print(f"       wejście:     {raw!r}")
            print(f"       oczekiwano:  {expected!r}")
            print(f"       otrzymano:   {got!r}")
    total = len(CASES)
    print(f"\n{total - failed}/{total} testów przeszło")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
