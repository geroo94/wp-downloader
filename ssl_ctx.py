"""ssl_ctx.py — kontekst TLS działający także w zamrożonej aplikacji.

PROBLEM: PyInstaller nie wnosi magazynu certyfikatów systemu, a OpenSSL szuka
go pod ścieżkami zapisanymi w Pythonie przy jego kompilacji. Na maszynie, gdzie
tych ścieżek nie ma (typowo macOS bez `/etc/ssl/cert.pem`), domyślny kontekst
SSL ma ZERO zaufanych certyfikatów i KAŻDE połączenie `https://` kończy się:

    [SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed:
    unable to get local issuer certificate

Zweryfikowane pomiarem, nie domysłem: przy niedostępnym magazynie systemowym
`ssl.create_default_context().cert_store_stats()` zwraca `{'x509': 0,
'x509_ca': 0}`, a ten sam kontekst zbudowany z certifi — 120 urzędów i HTTP 200
z api.github.com.

ROZWIĄZANIE: bierzemy paczkę certyfikatów `certifi`, którą PyInstaller pakuje
razem z aplikacją (w bundlu: `Contents/Resources/certifi/cacert.pem`), a gdy
jej nie ma — spadamy na magazyn systemowy.

CZEGO TU CELOWO NIE MA: wariantu z `check_hostname = False` i `verify_mode =
ssl.CERT_NONE`. Tymi połączeniami leci paczka, która podmienia działającą
aplikację, oraz instalatory komponentów systemowych. Bez weryfikacji
certyfikatu ktokolwiek w tej samej sieci mógłby podstawić własny plik
wykonywalny, a aplikacja podpisałaby go ad-hoc i uruchomiła. Gdy nie ma żadnego
zaufanego magazynu, poprawną odpowiedzią jest czytelny błąd i pobranie wydania
przeglądarką — nie ciche zdjęcie ochrony.
"""

from __future__ import annotations

import logging
import os
import ssl
from functools import lru_cache
from typing import Optional

logger = logging.getLogger(__name__)


def certifi_cafile() -> Optional[str]:
    """Ścieżka do `cacert.pem` z certifi albo None, gdy paczki nie ma.

    Sprawdzamy `isfile`, bo `certifi.where()` w zamrożonej aplikacji zwraca
    ścieżkę w bundlu — jeżeli build zgubiłby `datas`, dostalibyśmy nazwę pliku,
    którego na dysku nie ma, i `create_default_context` wywaliłoby się na
    FileNotFoundError.
    """
    try:
        import certifi
    except Exception:
        return None
    try:
        path = certifi.where()
    except Exception:
        return None
    return path if path and os.path.isfile(path) else None


@lru_cache(maxsize=1)
def secure_ssl_context() -> ssl.SSLContext:
    """Kontekst TLS z PEŁNĄ weryfikacją, odporny na brak magazynu systemowego.

    Wynik jest cache'owany: wczytanie ~120 certyfikatów przy każdym zapytaniu
    byłoby marnotrawstwem, a kontekst można bezpiecznie współdzielić między
    połączeniami.
    """
    cafile = certifi_cafile()
    if cafile:
        try:
            ctx = ssl.create_default_context(cafile=cafile)
            logger.debug("TLS: magazyn CA z certifi (%s)", cafile)
            return ctx
        except Exception:
            logger.warning("TLS: nie udało się wczytać certifi z %s", cafile,
                           exc_info=True)

    ctx = ssl.create_default_context()
    if not ctx.cert_store_stats().get("x509_ca"):
        # Nie udajemy, że jest dobrze — bez tego ostrzeżenia diagnoza sprowadza
        # się do gołego CERTIFICATE_VERIFY_FAILED gdzieś w logu.
        logger.warning(
            "TLS: brak zaufanych certyfikatów (ani certifi, ani magazyn "
            "systemowy) — połączenia https:// będą odrzucane."
        )
    return ctx


def is_certificate_error(exc: BaseException) -> bool:
    """Czy wyjątek to nieudana weryfikacja certyfikatu?

    `urlopen` opakowuje błąd TLS w `URLError`, więc samo `isinstance` na
    wierzchnim wyjątku nie wystarcza — trzeba zajrzeć w `reason`.
    """
    if isinstance(exc, ssl.SSLCertVerificationError):
        return True
    reason = getattr(exc, "reason", None)
    return isinstance(reason, ssl.SSLCertVerificationError)
