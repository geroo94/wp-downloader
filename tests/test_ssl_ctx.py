"""Testy kontekstu TLS używanego przez updater i sprawdzanie komponentów.

Pilnują dwóch rzeczy naraz:
  * że aplikacja MA zaufane certyfikaty również wtedy, gdy systemowy magazyn CA
    jest nieosiągalny (to jest zgłoszony błąd: w spakowanej wersji na macOS
    każde https:// kończyło się CERTIFICATE_VERIFY_FAILED),
  * że nikt nie „naprawi" tego ponownie przez wyłączenie weryfikacji — tym
    kanałem leci paczka podmieniająca działającą aplikację, więc CERT_NONE
    byłoby zaproszeniem do podstawienia własnego pliku wykonywalnego.

Uruchomienie: python3 tests/test_ssl_ctx.py
"""

import os
import ssl
import subprocess
import sys
import urllib.error

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ssl_ctx import certifi_cafile, is_certificate_error, secure_ssl_context  # noqa: E402

failures: list[str] = []


def check(cond: bool, label: str) -> None:
    print(("  OK   " if cond else "  FAIL ") + label)
    if not cond:
        failures.append(label)


print("=== kontekst ma zaufane certyfikaty ===")
ctx = secure_ssl_context()
stats = ctx.cert_store_stats()
print(f"  (załadowanych urzędów: {stats.get('x509_ca')})")
check(stats.get("x509_ca", 0) > 0, "kontekst zawiera certyfikaty CA")
check(certifi_cafile() is not None, "paczka certifi jest dostępna")

print("\n=== weryfikacja NIE jest wyłączona ===")
check(ctx.verify_mode == ssl.CERT_REQUIRED, "verify_mode == CERT_REQUIRED")
check(ctx.check_hostname is True, "check_hostname włączone")

print("\n=== rozpoznawanie błędu certyfikatu ===")
wrapped = urllib.error.URLError(
    ssl.SSLCertVerificationError(1, "certificate verify failed"))
check(is_certificate_error(wrapped), "URLError opakowujący błąd certyfikatu")
check(is_certificate_error(ssl.SSLCertVerificationError(1, "x")), "goły błąd certyfikatu")
check(not is_certificate_error(TimeoutError("timeout")), "zwykły timeout to nie certyfikat")

print("\n=== scenariusz z zgłoszenia: brak systemowego magazynu CA ===")
# Podprocesem, bo magazyn OpenSSL ustala się przy tworzeniu kontekstu, a ten
# jest w module cache'owany. SSL_CERT_FILE/DIR wskazujące w próżnię odtwarzają
# dokładnie to, co widzi zamrożona aplikacja na maszynie bez /etc/ssl/cert.pem.
env = dict(os.environ, SSL_CERT_FILE="/nonexistent/cert.pem", SSL_CERT_DIR="/nonexistent")
root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
probe = subprocess.run(
    [sys.executable, "-c",
     "import sys, ssl; sys.path.insert(0, %r);"
     "from ssl_ctx import secure_ssl_context;"
     "d=ssl.create_default_context().cert_store_stats()['x509_ca'];"
     "c=secure_ssl_context().cert_store_stats()['x509_ca'];"
     "print(d, c)" % root],
    capture_output=True, text=True, env=env,
)
if probe.returncode != 0:
    print("  (podproces zwrócił błąd)", probe.stderr.strip()[:200])
    failures.append("podproces diagnostyczny nie wystartował")
else:
    domyslny, nasz = (int(x) for x in probe.stdout.split())
    print(f"  (domyślny kontekst: {domyslny} CA | nasz: {nasz} CA)")
    check(domyslny == 0, "domyślny kontekst faktycznie traci certyfikaty (odtworzony błąd)")
    check(nasz > 0, "nasz kontekst nadal ma certyfikaty (poprawka działa)")

print("\n" + ("ALL PASS" if not failures else "FAIL: " + "; ".join(failures)))
sys.exit(1 if failures else 0)
