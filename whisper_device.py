"""whisper_device.py — detekcja i wybór urządzenia obliczeniowego (CPU/GPU)
dla silnika transkrypcji Whisper.

Trzy tryby (persystowane w config.json przez assets_manager, klucz
"whisper_device" — patrz get/set_whisper_device_pref):
  - "auto" (domyślny): najlepsze dostępne urządzenie, w kolejności CUDA > MPS > CPU.
  - "gpu": wymuś GPU — NVIDIA CUDA na Windows/Linux, Apple Silicon MPS na macOS;
    gdy żadne GPU nie jest dostępne, BEZPIECZNY fallback na CPU zamiast crasha
    (whisper.load_model(device="cuda") na maszynie bez CUDA rzuca wyjątkiem).
  - "cpu": wymuś CPU niezależnie od dostępnego GPU.

fp16 (float16) włączane WYŁĄCZNIE dla CUDA. Na CPU fp16 bywa niestabilne
(patrz istniejący komentarz w server.py). Na MPS openai-whisper historycznie
miewa niepełne wsparcie fp16 dla części operacji (RuntimeError na starszych
kombinacjach torch/macOS) — bezpieczniej zostawić float32 też tam, korzyść
z fp16 na MPS jest zresztą marginalna w porównaniu do CUDA.
"""

from __future__ import annotations

import logging
from functools import lru_cache

logger = logging.getLogger(__name__)

_VALID_PREFS = ("auto", "gpu", "cpu")


@lru_cache(maxsize=1)
def detect_hardware() -> dict:
    """Sonduje dostępny sprzęt obliczeniowy. Import torch jest leniwy i
    osłonięty try/except — biblioteka jest ciężka (~2 GB, patrz komentarz przy
    _bundled_whisper_dir w server.py) i bywa nieobecna w odchudzonych env-ach
    dev/CI bez zależności Whispera.

    @lru_cache(maxsize=1): sprzęt/sterowniki GPU nie zmieniają się w trakcie
    życia procesu, więc sonda (import torch + cuda/mps probing) ma sens tylko
    RAZ — bez cache'a powtarzałaby się przy każdej transkrypcji i każdym
    GET/POST /api/settings/whisper-device."""
    info = {"cuda": False, "cuda_name": None, "mps": False}
    try:
        import torch

        if torch.cuda.is_available():
            info["cuda"] = True
            try:
                info["cuda_name"] = torch.cuda.get_device_name(0)
            except Exception:
                pass
        # torch.backends.mps istnieje tylko w buildach z obsługą Apple Silicon —
        # getattr zamiast bezpośredniego dostępu, żeby nie wywalić się na
        # buildach torch bez tego backendu (np. część kompilacji CPU-only).
        mps_backend = getattr(torch.backends, "mps", None)
        if mps_backend is not None and mps_backend.is_available():
            info["mps"] = True
    except Exception as exc:
        logger.warning("whisper_device: detekcja sprzętu nie powiodła się: %s", exc)
    return info


def resolve_device(pref: str) -> tuple[str, str, bool]:
    """Zwraca (torch_device, etykieta_ui, fp16) dla zadanej preferencji
    ("auto" | "gpu" | "cpu" — inne wartości traktowane jak "auto")."""
    pref = pref if pref in _VALID_PREFS else "auto"
    hw = detect_hardware()

    def _best_gpu() -> tuple[str, str] | None:
        if hw["cuda"]:
            name = hw["cuda_name"] or "NVIDIA GPU"
            return "cuda", f"{name} (CUDA)"
        if hw["mps"]:
            return "mps", "Apple Silicon (Metal/MPS)"
        return None

    if pref == "cpu":
        return "cpu", "CPU (wielowątkowe, wymuszone)", False

    gpu = _best_gpu()
    if pref == "gpu":
        if gpu:
            device, label = gpu
            return device, label, device == "cuda"
        logger.warning(
            "whisper_device: wymuszono GPU, ale brak CUDA/MPS na tej maszynie — "
            "fallback na CPU (bez tego load_model(device=...) rzuciłby wyjątkiem)")
        return "cpu", "CPU (wielowątkowe — brak GPU mimo wymuszenia)", False

    # "auto"
    if gpu:
        device, label = gpu
        return device, label, device == "cuda"
    return "cpu", "CPU (wielowątkowe)", False


def current_status() -> dict:
    """Snapshot dla frontendu: {pref, device, label, fp16, hardware}. Preferencja
    czytana bezpośrednio z assets_manager (jedyny właściciel config.json) —
    ten moduł zajmuje się WYŁĄCZNIE detekcją/rozstrzyganiem sprzętu, nie
    persystencją."""
    from assets_manager import get_whisper_device_pref

    pref = get_whisper_device_pref()
    device, label, fp16 = resolve_device(pref)
    return {
        "pref": pref,
        "device": device,
        "label": label,
        "fp16": fp16,
        "hardware": detect_hardware(),  # @lru_cache — nie re-sonduje sprzętu
    }
