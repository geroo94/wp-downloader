"""
ServerThread — uruchamia serwer FastAPI w osobnym wątku.

Dlaczego osobny wątek?
- Qt (okno aplikacji) musi działać w głównym wątku
- FastAPI/uvicorn też chce głównego wątku
- Rozwiązanie: FastAPI dostaje swój własny wątek, Qt zostaje w głównym

Wątek = jak drugi pracownik w tym samym biurze.
Oboje pracują jednocześnie, ale na różnych zadaniach.
"""

import threading
import asyncio
import sys

import uvicorn

# Wymagane do /ws (live updates). Bez tego uvicorn loguje „Unsupported upgrade request”.
try:
    import websockets  # noqa: F401
except ImportError:
    print(
        "BŁĄD: brak pakietu 'websockets'. Uruchom: pip install -r requirements.txt",
        file=sys.stderr,
    )
    raise

from download_manager import DownloadManager
from server import create_app

PORT = 8765  # port na którym nasłuchuje serwer (localhost:8765)


class ServerThread(threading.Thread):
    """
    Wątek który uruchamia serwer uvicorn.
    
    Dziedziczy po threading.Thread — to znaczy że jest wątkiem.
    Musimy nadpisać metodę run() która jest wywołana po .start()
    """

    def __init__(self, manager: DownloadManager):
        # Wywołaj konstruktor klasy nadrzędnej (threading.Thread)
        super().__init__(daemon=True)  # daemon=True: wątek umrze gdy zamkniemy aplikację

        self.manager = manager
        self.loop = None    # pętla asyncio dla tego wątku
        self.server = None  # instancja uvicorn.Server

    def run(self):
        """
        Ta metoda jest wywołana gdy wątek startuje (.start()).
        Tworzy nową pętlę asyncio i uruchamia w niej serwer.
        """
        # Każdy wątek potrzebuje własnej pętli asyncio
        # (główny wątek ma swoją, ten wątek ma swoją)
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)

        # Stwórz aplikację FastAPI
        app = create_app(self.manager)

        # Konfiguracja uvicorn (serwer HTTP)
        config = uvicorn.Config(
            app=app,
            host="127.0.0.1",   # tylko lokalnie, nie wystawiamy na internet
            port=PORT,
            log_level="warning",  # nie zaśmiecaj konsoli
            loop="asyncio",
            ws="websockets",  # jawny backend WS (nie „auto” w wątku z własną pętlą)
        )

        self.server = uvicorn.Server(config)

        # Uruchom serwer (blokuje ten wątek — to ok, działa w tle)
        self.loop.run_until_complete(self.server.serve())

    def stop(self):
        """Zatrzymuje serwer i pętlę asyncio."""
        if self.server and self.loop:
            # Wywołujemy shutdown i czekamy na zakończenie.
            # Gwarantuje to, że blok 'lifespan' w server.py zdąży ubić procesy yt-dlp.
            future = asyncio.run_coroutine_threadsafe(
                self.server.shutdown(),
                self.loop
            )
            try:
                # Czekamy maksymalnie 5 sekund na czyste zamknięcie serwera i workera
                future.result(timeout=5)
            except Exception:
                pass
            
            self.loop.call_soon_threadsafe(self.loop.stop)
