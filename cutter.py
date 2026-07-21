"""cutter.py — Fast Cutter FFmpeg pipeline.

Dwa scenariusze:
  A. **Supersoniczny** (`-c copy`): brak brandingu (logo/src_text/outro). Kopiuje
     surowy stream bez dekodowania. Trwa 2-3 s. `-ss` przed `-i` = fast seek na
     najbliższą keyframę (~2 s odchyłka akceptowalna dla trybu quick).
  B. **Branded** (filter_complex + GPU): overlay logo → drawtext źródła →
     opcjonalny concat outro. Encoder per platforma:
       Windows → h264_nvenc (Nvidia GPU)
       macOS   → h264_videotoolbox (Apple GPU)
       inne    → libx264 (CPU fallback)

Progress: parsujemy `time=HH:MM:SS.ff` i `speed=X.Yx` ze stderr ffmpeg.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
import uuid
from dataclasses import dataclass, field
from typing import Optional

from binaries import get_ffmpeg, get_ffprobe, subprocess_flags

logger = logging.getLogger(__name__)


def _get_resource_path(rel: str) -> str:
    """Ścieżka do zasobu — działa w dev i w frozen PyInstaller `.app`/`.exe`.
    Duplikat z main.get_resource_path (import z main sięga circular jeśli main
    nie jest jeszcze zainicjalizowany przy pierwszym imporcie cutter)."""
    if hasattr(sys, "_MEIPASS"):
        return os.path.join(sys._MEIPASS, rel)
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), rel)


def _default_logo_path() -> str:
    return _get_resource_path(os.path.join("static", "branding", "default_logo.png"))


def _default_outro_path() -> str:
    return _get_resource_path(os.path.join("static", "branding", "default_outro.mp4"))


def _default_sub_path() -> str:
    """Animowany przycisk subskrypcji (CineForm RGB+alfa, ~10 s)."""
    return _get_resource_path(os.path.join("static", "branding", "sub_button.mov"))


def _alpha_decoder_args(meta: dict | None) -> list[str]:
    """`-c:v <dekoder>` przed `-i` dla kodeków, których natywne dekodery
    ffmpeg gubią kanał alfa (VP8/VP9 w WebM trzymają alfę w side-dacie
    czytanej tylko przez libvpx). CineForm/ProRes/qtrle dekodują alfę
    natywnie — pusta lista."""
    vdec = {"vp9": "libvpx-vp9", "vp8": "libvpx"}.get((meta or {}).get("vcodec", ""))
    return ["-c:v", vdec] if vdec else []


def _hw_encoder() -> tuple[str, list[str]]:
    """Zwraca (codec, extra_args) dla platformy. Fallback: libx264 CPU."""
    if sys.platform == "win32":
        # NVENC: p1 (najszybszy, najniższa jakość) → p7 (najlepszy). p4 = balanced.
        # -cq 23 = jakość ~equivalent do libx264 -crf 23. VBR z sensownym cap.
        return "h264_nvenc", [
            "-preset", "p4", "-cq", "23", "-rc", "vbr",
            "-b:v", "5M", "-maxrate", "8M",
        ]
    if sys.platform == "darwin":
        # VideoToolbox: -allow_sw 1 pozwala fallback na software gdy GPU zajęte.
        return "h264_videotoolbox", ["-b:v", "5M", "-allow_sw", "1"]
    return "libx264", ["-preset", "veryfast", "-crf", "23"]


def _drawtext_fontfile() -> str:
    """Czcionka dla ffmpeg drawtext: bundlowana Gilroy SemiBold z brandingu
    (rejestrowana w .spec), z fallbackiem na czcionki systemowe gdyby zasób
    zniknął. Zwraca SUROWĄ ścieżkę — escaping robi _filter_path_escape()."""
    gilroy = _get_resource_path(
        os.path.join("static", "branding", "fonts", "Gilroy-SemiBold.ttf"))
    if os.path.isfile(gilroy):
        return gilroy
    if sys.platform == "win32":
        return "C:/Windows/Fonts/arial.ttf"
    if sys.platform == "darwin":
        return "/System/Library/Fonts/Helvetica.ttc"
    return "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"


_FONT_CANDIDATES = {
    "arial": [
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/Library/Fonts/Arial.ttf",
        "C:/Windows/Fonts/arial.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    ],
    "opensans": [
        "/Library/Fonts/Open Sans.ttf",
        os.path.expanduser("~/Library/Fonts/OpenSans-Regular.ttf"),
        "C:/Windows/Fonts/OpenSans-Regular.ttf",
        "/usr/share/fonts/truetype/open-sans/OpenSans-Regular.ttf",
    ],
}


def _font_path(font_id: str) -> str:
    """Rozwiązuje identyfikator czcionki z dropdownu "Wybór czcionki" (Fast
    Cutter, pole Źródło) na bezwzględną ścieżkę pliku. "gilroy" (domyślna)
    lub nieznany identyfikator → bundlowana Gilroy. Dla czcionek systemowych
    (Arial/Open Sans) próbujemy typowe lokalizacje per-platforma; gdy żadna
    nie istnieje na tej maszynie, cichy fallback do Gilroy — render nigdy
    nie ma paść przez brakujący plik fontfile."""
    gilroy = _drawtext_fontfile()
    for c in _FONT_CANDIDATES.get(font_id or "gilroy", []):
        if os.path.isfile(c):
            return c
    return gilroy


def _filter_path_escape(p: str) -> str:
    """Ścieżka do wnętrza filtergraphu (fontfile=, sendcmd f=): forward
    slashe + dwukropek escaped (`C\\:` na Windows), inaczej parser filtrów
    tnie ścieżkę na dwukropku."""
    return p.replace("\\", "/").replace(":", r"\:")


def _bundled_ffmpeg() -> str:
    """Awaryjna statyczna binarka ffmpeg z pakietu imageio-ffmpeg (pełny
    zestaw filtrów, w tym drawtext/sendcmd). Używana tylko gdyby bundlowany
    `bin/ffmpeg` z jakiegoś powodu nie miał drawtext. Pusta gdy niedostępna."""
    try:
        import imageio_ffmpeg
        p = imageio_ffmpeg.get_ffmpeg_exe()
        return p if p and os.path.isfile(p) else ""
    except Exception:
        return ""


@dataclass
class CutterJob:
    request_id: str
    input_path: str
    start_ts: float
    end_ts: float
    logo_src: str = ""              # "" | "default" | "custom"
    logo_pos: str = "tr"            # tr | tl | br | bl
    logo_custom_path: str = ""
    # Suwaki brandingu: skala jako ułamek szerokości materiału (0.05–0.40),
    # marginesy X/Y w pikselach źródła od wybranego rogu.
    logo_scale: float = 0.16
    logo_x: int = 20
    logo_y: int = 20
    src_text: str = ""
    # Formatowanie źródła: rozmiar czcionki (px względem kadru 1080p,
    # skalowany do realnej wysokości), narożnik (tl = bazowy) i marginesy
    # X/Y od narożnika (px w układzie 1920x1080, skalowane do kadru).
    src_size: int = 28
    src_pos: str = "tl"             # tl | tr | bl | br
    src_x: int = 24
    src_y: int = 24
    src_font: str = "gilroy"        # gilroy | arial | opensans
    # Ustawienia → "Wyłącz animacje interfejsu": skip efekt maszyny do pisania,
    # pełny tekst źródła renderuje się statycznie od pierwszej klatki.
    disable_typewriter: bool = False
    outro_src: str = ""             # "" | "default" | "custom"
    outro_custom_path: str = ""
    # Overlap: outro (z maską alfa, jeśli ją ma) nakładane overlay'em X sekund
    # przed końcem wycinka zamiast doklejane po nim. 0 = klasyczny concat.
    outro_overlap: float = 0.0
    # Mixer: głośność głównego materiału (0.0–2.0; 1.0 = bez zmian).
    # Nie dotyka audio outro — tyłówka gra swoim poziomem.
    audio_volume: float = 1.0
    # Animowany przycisk subskrypcji (alfa): overlay na starcie wycinka
    # i drugi raz tak, by skończył się dokładnie przed wejściem outro.
    sub_overlay: bool = False
    # Własny plik animacji SUB (alternatywa dla bundlowanego sub_button.mov).
    # Puste lub nieistniejący plik → fallback do domyślnego.
    sub_custom_path: str = ""
    # Link do zadania w DownloadManager — pozwala propagować progress do UI
    # Historii pobierania (przez WebSocket task_update). Pusty = tryb standalone.
    download_task_id: str = ""
    # runtime
    status: str = "queued"          # queued | rendering | done | error
    progress: int = 0
    speed: str = ""
    output_path: str = ""
    error: str = ""
    proc: Optional[asyncio.subprocess.Process] = field(default=None, repr=False)
    # Pliki tymczasowe renderu (np. sendcmd typewritera) — sprzątane w _run().
    tmp_files: list = field(default_factory=list, repr=False)


class CutterManager:
    """In-memory registry ffmpeg jobs. Trzyma referencje do live processów
    dla graceful stop (bez SIGKILL — dajemy ffmpeg czas na finalize moov atom).

    Opcjonalny `manager` (DownloadManager) — pozwala propagować progress do
    UI przez update_task/broadcast. Bez managera CutterJob żyje standalone."""

    def __init__(self, manager=None) -> None:
        self.jobs: dict[str, CutterJob] = {}
        self._manager = manager
        # Cache możliwości binarek ffmpeg: klucz (binarka, "f:"|"e:" + nazwa).
        # PATH-owy ffmpeg bywa okrojony (brew bez libfreetype = brak drawtext),
        # a bundlowana statyczna binarka może nie mieć enkoderów HW.
        self._cap_cache: dict[tuple, bool] = {}

    async def _probe_caps(self, kind_flag: str, name: str, binary: str) -> bool:
        key = (binary, kind_flag, name)
        if key in self._cap_cache:
            return self._cap_cache[key]
        ok = True
        try:
            proc = await asyncio.create_subprocess_exec(
                binary, "-hide_banner", kind_flag,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                creationflags=subprocess_flags(),
            )
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
            ok = re.search(rf"\s{re.escape(name)}\s",
                           out.decode("utf-8", "replace")) is not None
        except Exception as exc:
            logger.debug("cutter caps probe fail (%s %s @ %s): %s",
                         kind_flag, name, binary, exc)
        self._cap_cache[key] = ok
        return ok

    async def _has_filter(self, name: str, binary: str | None = None) -> bool:
        """Czy dana binarka ffmpeg ma filtr. Optymistyczne True przy błędzie
        sondy — najwyżej render padnie z czytelnym logiem."""
        return await self._probe_caps("-filters", name, binary or get_ffmpeg())

    async def _has_encoder(self, name: str, binary: str | None = None) -> bool:
        return await self._probe_caps("-encoders", name, binary or get_ffmpeg())

    # ── Asset resolution ────────────────────────────────────────────────

    def _resolve_logo(self, job: CutterJob) -> str:
        if job.logo_src == "default":
            p = _default_logo_path()
            return p if os.path.isfile(p) else ""
        if job.logo_src == "custom" and job.logo_custom_path and os.path.isfile(job.logo_custom_path):
            return job.logo_custom_path
        return ""

    def _resolve_outro(self, job: CutterJob) -> str:
        if job.outro_src == "default":
            p = _default_outro_path()
            return p if os.path.isfile(p) else ""
        if job.outro_src == "custom" and job.outro_custom_path and os.path.isfile(job.outro_custom_path):
            return job.outro_custom_path
        return ""

    def _output_path(self, job: CutterJob) -> str:
        base, _ = os.path.splitext(job.input_path)
        # Sufiks pokazuje zakres w sekundach — użyteczne przy wielokrotnym cięciu.
        return f"{base}_cut_{int(job.start_ts)}-{int(job.end_ts)}.mp4"

    def _resolve_sub(self, job: CutterJob) -> str:
        if not job.sub_overlay:
            return ""
        if job.sub_custom_path and os.path.isfile(job.sub_custom_path):
            return job.sub_custom_path
        p = _default_sub_path()
        return p if os.path.isfile(p) else ""

    def _has_branding(self, job: CutterJob) -> bool:
        # Głośność celowo NIE wchodzi do brandingu: sama zmiana audio nie
        # wymaga re-encode wideo (tryb copy przełącza tylko ścieżkę audio).
        return bool(self._resolve_logo(job) or job.src_text.strip()
                    or self._resolve_outro(job) or self._resolve_sub(job))

    @staticmethod
    def _volume_value(job: CutterJob) -> float:
        """Głośność z clampem 0–2; wartości ~1.0 traktujemy jako brak filtra."""
        try:
            v = float(job.audio_volume)
        except (TypeError, ValueError):
            return 1.0
        v = min(2.0, max(0.0, v))
        return 1.0 if abs(v - 1.0) < 0.01 else v

    def _pos_overlay(self, pos: str, mx: int = 20, my: int = 20) -> str:
        # Marginesy mx/my od wybranego rogu. W = width tła, w = width logo
        # (analogicznie H/h). Wartości sterowane suwakami Pozycja X/Y.
        mx, my = max(0, int(mx)), max(0, int(my))
        return {
            "tr": f"W-w-{mx}:{my}",
            "tl": f"{mx}:{my}",
            "br": f"W-w-{mx}:H-h-{my}",
            "bl": f"{mx}:H-h-{my}",
        }.get(pos, f"W-w-{mx}:{my}")

    # ── Media probing ───────────────────────────────────────────────────

    async def _probe_media(self, path: str) -> dict:
        """ffprobe metadanych pliku: wymiary, fps, audio, duration.

        Wynik napędza builder filtrów branded: logo skalujemy względem
        szerokości wejścia, outro do dokładnej rozdzielczości/fps wejścia
        (concat wymaga zgodnych parametrów), a brak ścieżki audio zastępujemy
        anullsrc. Fallback na sensowne defaulty gdy ffprobe padnie —
        render wtedy nadal ruszy, co najwyżej z domyślnym 1080p."""
        default = {"w": 1920, "h": 1080, "fps": 30.0, "has_audio": True, "duration": 0.0,
                   "vcodec": ""}
        cmd = [get_ffprobe(), "-v", "error",
               "-show_entries",
               "format=duration:stream=codec_type,codec_name,width,height,avg_frame_rate",
               "-of", "json", path]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                creationflags=subprocess_flags(),
            )
            out, _ = await proc.communicate()
            if proc.returncode != 0:
                return default
            data = json.loads(out.decode("utf-8", "replace"))
            meta = dict(default)
            meta["has_audio"] = False
            try:
                # ffprobe potrafi zwrócić "N/A" (np. surowe strumienie)
                meta["duration"] = float(data.get("format", {}).get("duration", 0) or 0)
            except (TypeError, ValueError):
                meta["duration"] = 0.0
            for st in data.get("streams", []):
                ctype = st.get("codec_type")
                if ctype == "video" and st.get("width"):
                    meta["w"] = int(st.get("width") or 1920)
                    meta["h"] = int(st.get("height") or 1080)
                    meta["vcodec"] = st.get("codec_name") or ""
                    fr = st.get("avg_frame_rate") or "30/1"
                    try:
                        num, den = fr.split("/")
                        fps = float(num) / float(den or 1)
                        if 1.0 <= fps <= 240.0:
                            meta["fps"] = fps
                    except (ValueError, ZeroDivisionError):
                        pass
                elif ctype == "audio":
                    meta["has_audio"] = True
            return meta
        except Exception as exc:
            logger.debug("cutter probe fail (%s): %s", path, exc)
            return default

    # ── Command builders ────────────────────────────────────────────────

    def _cmd_copy(self, job: CutterJob) -> list[str]:
        """Scenariusz A: stream copy. -ss przed -i = fast seek keyframe-aligned.

        Używamy `-t <dur>` zamiast `-to <end>` — input-side `-to` po seek'u
        jest w nowszych ffmpeg (8.x) liczone względem punktu seeka, nie
        oryginalnych timestampów, co dawało za długie wycinki.

        Mixer: przy głośności ≠ 100% wideo dalej leci stream-copy, ale audio
        przechodzi przez volume + re-encode AAC (nie da się filtrować
        skopiowanego bitstreamu)."""
        dur = max(0.1, job.end_ts - job.start_ts)
        vol = self._volume_value(job)
        if vol != 1.0:
            audio_args = ["-c:v", "copy",
                          "-filter:a", f"volume={vol:.3f}",
                          "-c:a", "aac", "-b:a", "192k"]
        else:
            audio_args = ["-c", "copy"]
        return [
            get_ffmpeg(), "-hide_banner", "-y",
            "-ss", f"{job.start_ts:.3f}",
            "-t", f"{dur:.3f}",
            "-i", job.input_path,
            *audio_args,
            "-avoid_negative_ts", "make_zero",
            "-movflags", "+faststart",
            job.output_path,
        ]

    def _cmd_reencode(self, job: CutterJob) -> list[str]:
        """Fallback dla trybu copy: pełny re-encode bez brandingu.

        Używany gdy `-c copy` padnie (np. VP9/Opus z .webm nie wchodzi w .mp4
        bez transkodowania albo cięcie poza keyframe daje uszkodzony plik)."""
        codec, codec_args = _hw_encoder()
        dur = max(0.1, job.end_ts - job.start_ts)
        vol = self._volume_value(job)
        vol_args = ["-filter:a", f"volume={vol:.3f}"] if vol != 1.0 else []
        return [
            get_ffmpeg(), "-hide_banner", "-y",
            "-ss", f"{job.start_ts:.3f}",
            "-t", f"{dur:.3f}",
            "-i", job.input_path,
            "-c:v", codec, *codec_args,
            *vol_args,
            "-c:a", "aac", "-b:a", "192k",
            "-pix_fmt", "yuv420p",
            "-movflags", "+faststart",
            job.output_path,
        ]

    def _cmd_branded(self, job: CutterJob, meta: dict, outro_meta: dict | None,
                     sub_meta: dict | None = None, force_sw: bool = False) -> list[str]:
        """Scenariusz B: filter_complex z overlay + drawtext + concat outro.

        `-ss` PRZED `-i` + `-t` = szybki seek do fragmentu bez dekodowania
        całego materiału od zera (przy re-encode ffmpeg i tak dekoduje od
        poprzedniej keyframe i odrzuca klatki do zadanego punktu, więc cięcie
        pozostaje frame-accurate). Timestampy po input-seek zaczynają się od 0,
        co daje stabilny timebase dla filter_complex i concat.

        force_sw: bundlowana statyczna binarka może nie mieć enkodera HW
        platformy ALBO enkoder GPU zawiódł w runtime (np. nvenc zwraca
        "-40 Function not implemented", gdy sterowniki/GPU nie wspierają
        enkodowania mimo że -encoders go wypisuje) — w obu przypadkach render
        idzie na niezawodnym CPU: libx264 -preset medium (patrz start(),
        które dorzuca ten wariant jako automatyczny fallback w cmds)."""
        if force_sw:
            codec, codec_args = "libx264", ["-preset", "medium", "-crf", "23"]
        else:
            codec, codec_args = _hw_encoder()
        logo = self._resolve_logo(job)
        outro = self._resolve_outro(job)
        sub = self._resolve_sub(job)
        src_text = job.src_text.strip()
        dur = max(0.1, job.end_ts - job.start_ts)

        main_w, main_h = int(meta["w"]), int(meta["h"])
        fps = float(meta["fps"])
        fps_s = f"{fps:.6g}"

        # Suwaki logo/tekstu źródła są kalibrowane w px względem referencyjnego
        # kadru 1920x1080. Skalowanie osobno po X (main_w/1920) i osobno po Y
        # (main_h/1080) daje SPÓJNY współczynnik tylko dla wideo 16:9 — przy
        # innej proporcji (zwłaszcza pionowej, np. 1080x1920) oba współczynniki
        # rozjeżdżają się nawet 3x (0.56 vs 1.78), więc marginesy/fontsize
        # wychodzą poza kadr albo kolidują z krawędzią. Jeden współczynnik
        # liczony z KRÓTSZEGO boku (ograniczający wymiar w każdej orientacji,
        # zgodnie z min(W,H)) eliminuje ten rozjazd i jest identyczny jak
        # dotychczas dla dowolnego wideo 16:9 (min(1920,1080)/1080 == 1.0).
        ref_scale = min(main_w, main_h) / 1080.0

        # ── Timing warstwy Outro (compositing, nie doklejanie) ──────────
        # Formuła: outro_start = dur - od + delay. Delay=0 → CAŁE outro
        # nałożone na końcówkę materiału (kończy się razem z nim); delay=od
        # → outro w całości za końcem (czyste doklejenie). Ogon wystający
        # za koniec materiału (tail) = delay, dokładany czarną dokładką.
        od = ov = 0.0
        outro_start = dur
        outro_tail = 0.0
        if outro:
            od = max(0.1, float((outro_meta or {}).get("duration") or 5.0))
            ov = self._overlap_value(job, dur, od)
            outro_start = max(0.0, dur - od + ov)
            outro_tail = max(0.0, (outro_start + od) - dur)

        # Okna czasowe animacji subskrypcji: [0, sd] na starcie wycinka oraz
        # druga instancja kończąca się DOKŁADNIE na wejściu outro:
        # t2 = [outro_start - sd, outro_start]. Rygorystycznie: SUB nie może
        # nachodzić na czas trwania tyłówki ani wyświetlać się pod nią
        # (zgłoszony bug podwójnego nałożenia na końcówce). Bez outro
        # outro_start == dur, więc druga instancja domyka się z końcem klipu.
        sub_legs: list[tuple[float, float]] = []
        if sub and sub_meta:
            sd = max(0.2, min(float(sub_meta.get("duration") or 10.0), 30.0))
            w1_end = min(sd, dur)
            sub_legs.append((0.0, w1_end))
            t2_end = outro_start
            t2_start = t2_end - sd
            if t2_start > w1_end + 0.25:
                sub_legs.append((t2_start, t2_end))
            else:
                logger.info("cutter[%s]: wycinek za krótki (%.1fs) na drugą "
                            "instancję suba przed outro — zostaje tylko startowa",
                            job.request_id[:8], dur)

        inputs: list[str] = [
            "-ss", f"{job.start_ts:.3f}",
            "-t", f"{dur:.3f}",
            "-i", job.input_path,
        ]
        input_idx = 1
        logo_idx = outro_idx = silence_idx = outro_silence_idx = -1
        if logo:
            inputs += ["-i", logo]
            logo_idx = input_idx
            input_idx += 1
        if outro:
            inputs += _alpha_decoder_args(outro_meta) + ["-i", outro]
            outro_idx = input_idx
            input_idx += 1
        # Każda instancja suba = osobne wejście tego samego pliku (jeden
        # input można skonsumować w grafie tylko raz).
        sub_idxs: list[int] = []
        for _ in sub_legs:
            inputs += _alpha_decoder_args(sub_meta) + ["-i", sub]
            sub_idxs.append(input_idx)
            input_idx += 1
        # Brak audio w źródle / outro → cicha ścieżka z lavfi, żeby mapy
        # audio i concat (v=1:a=1) zawsze miały co skonsumować.
        if not meta["has_audio"]:
            inputs += ["-f", "lavfi", "-t", f"{dur:.3f}",
                       "-i", "anullsrc=channel_layout=stereo:sample_rate=48000"]
            silence_idx = input_idx
            input_idx += 1
        if outro and outro_meta is not None and not outro_meta["has_audio"]:
            outro_dur = max(0.1, float(outro_meta.get("duration") or 5.0))
            inputs += ["-f", "lavfi", "-t", f"{outro_dur:.3f}",
                       "-i", "anullsrc=channel_layout=stereo:sample_rate=48000"]
            outro_silence_idx = input_idx
            input_idx += 1
        # Ogon outro wystający za koniec materiału (= delay): czarna dokładka
        # wideo doklejana concat'em do głównego klipu — jedyny NIEZAWODNY
        # sposób przedłużenia strumienia z -ss/-t trimmed inputu
        # (tpad=stop_mode=clone milczkiem nie dokleja klatek na takim
        # wejściu — odtworzone empirycznie).
        black_idx = -1
        if outro and outro_tail > 0.05:
            inputs += ["-f", "lavfi", "-t", f"{outro_tail:.3f}",
                       "-i", f"color=black:size={main_w}x{main_h}:rate={fps_s}"]
            black_idx = input_idx
            input_idx += 1

        # Wspólny format audio dla obu segmentów — concat wymaga zgodnych
        # sample_rate/layout, a AAC enkoder i tak dostaje 48 kHz stereo.
        afmt = "aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo"

        fg: list[str] = [
            "[0:v]setpts=PTS-STARTPTS,format=yuv420p,setsar=1[v0]",
        ]
        # Mixer: volume tylko na głównym materiale — outro gra swoim poziomem.
        vol = self._volume_value(job)
        vol_f = f",volume={vol:.3f}" if vol != 1.0 else ""
        a_src = f"[{silence_idx}:a]" if silence_idx >= 0 else "[0:a]"
        fg.append(f"{a_src}asetpts=PTS-STARTPTS,{afmt}{vol_f}[a0]")
        base_v = "[v0]"
        base_a = "[a0]"

        if logo:
            if job.logo_src == "default":
                # Bazowe logo 1920x1080: przy skali 100% i zerowych
                # marginesach = dokładnie fullframe od 0:0 (pozycja zaszyta
                # w grafice). Suwaki są W PEŁNI aktywne: skala pomniejsza
                # całą grafikę (kotwica w prawym górnym rogu — tam siedzi
                # znak), a Pozycja X/Y odsuwa ją od krawędzi, gdy logo
                # nachodzi na branding zaszyty w materiale.
                s = min(1.0, max(0.05, float(job.logo_scale or 1.0)))
                lw = max(64, round(main_w * s))
                lh = max(36, round(main_h * s))
                dmx = max(0, round(max(0, int(job.logo_x)) * ref_scale))
                dmy = max(0, round(max(0, int(job.logo_y)) * ref_scale))
                fg.append(f"[{logo_idx}:v]scale={lw}:{lh}:"
                          f"force_original_aspect_ratio=decrease[lg]")
                fg.append(f"{base_v}[lg]overlay=W-w-{dmx}:{dmy}:format=auto[v1]")
            else:
                # Custom: skala z suwaka (ułamek szerokości, clamp 5–40%)
                # + marginesy X/Y od wybranego rogu.
                scale = min(0.40, max(0.05, float(job.logo_scale or 0.16)))
                logo_w = max(32, round(main_w * scale))
                pos = self._pos_overlay(job.logo_pos, job.logo_x * ref_scale, job.logo_y * ref_scale)
                fg.append(f"[{logo_idx}:v]scale={logo_w}:-1[lg]")
                fg.append(f"{base_v}[lg]overlay={pos}:format=auto[v1]")
            base_v = "[v1]"

        if src_text:
            # ── Źródło: Gilroy + narożnik z dropdownu + typewriting.
            # Tekst sanitizowany pod DWA parsery naraz (plik sendcmd →
            # argument reinit → parser opcji drawtext): apostrof zamieniamy
            # na typograficzny, średnik (terminator komendy sendcmd) na
            # przecinek, reszta klasycznym escapem drawtext.
            clean = (src_text.replace("\\", "")
                             .replace("'", "’")
                             .replace(";", ","))
            safe_full = clean.replace(":", r"\:").replace("%", r"\%")
            fontfile = _filter_path_escape(_font_path(job.src_font))
            size_px = min(96, max(12, int(job.src_size or 28)))
            fontsize = max(12, round(size_px * ref_scale))
            # Marginesy z suwaków Pozycja X/Y źródła (px @1920x1080, przez
            # ref_scale — patrz komentarz przy jego wyliczeniu wyżej)
            mx = max(0, round(max(0, int(job.src_x)) * ref_scale))
            my = max(0, round(max(0, int(job.src_y)) * ref_scale))
            pos_xy = {
                "tl": f"x={mx}:y={my}",
                "tr": f"x=w-tw-{mx}:y={my}",
                "bl": f"x={mx}:y=h-th-{my}",
                "br": f"x=w-tw-{mx}:y=h-th-{my}",
            }.get(job.src_pos or "tl", f"x={mx}:y={my}")

            if job.disable_typewriter:
                # Ustawienia → "Wyłącz animacje interfejsu": pełny tekst od
                # razu, bez sendcmd/reinit — statyczny drawtext.
                fg.append(
                    f"{base_v}drawtext=fontfile='{fontfile}':text='{safe_full}':"
                    f"fontcolor=white:fontsize={fontsize}:"
                    f"shadowx=2:shadowy=2:shadowcolor=black:"
                    f"{pos_xy}[v2]"
                )
                base_v = "[v2]"
                logger.debug("cutter[%s] źródło: %r size=%d pos=%s (statyczny, bez typewritera)",
                             job.request_id[:8], safe_full, fontsize, job.src_pos)
            else:
                # Typewriting dwufazowe: Faza 1 = sendcmd co 1/speed s robi
                # `drawtext@src reinit` z coraz dłuższym prefiksem; prędkość
                # min. 20 zn/s, przy długich tekstach rośnie tak, by animacja
                # zamknęła się w ≤3 s. Faza 2 = ostatni reinit zostawia PEŁNY
                # tekst statycznie do końca klipu (żadnych dalszych komend).
                TYPE_SPEED = max(20.0, len(clean) / 3.0)
                cmd_lines = []
                for i in range(1, len(clean) + 1):
                    # Cały argument reinit w '...' (kanoniczna forma z docs);
                    # dwukropek/procent w treści escapowane dla parsera opcji.
                    chunk = clean[:i].replace(":", r"\:").replace("%", r"\%")
                    cmd_lines.append(
                        f"{i / TYPE_SPEED:.3f} drawtext@src reinit 'text={chunk}';")
                # Po zakończeniu pisania tekst zostaje do końca (ostatni reinit).
                import tempfile
                with tempfile.NamedTemporaryFile(
                        "w", suffix=".sendcmd", delete=False, encoding="utf-8") as tf:
                    tf.write("\n".join(cmd_lines) + "\n")
                    sendcmd_path = tf.name
                job.tmp_files.append(sendcmd_path)

                # Bez box=1/boxcolor (brak czarnego tła pod tekstem) — tekst
                # leci bezpośrednio na obrazie, czytelność na jasnych kadrach
                # zapewnia delikatny cień (shadowx/y + shadowcolor) zamiast
                # pełnej ramki.
                fg.append(
                    f"{base_v}sendcmd=f='{_filter_path_escape(sendcmd_path)}',"
                    f"drawtext@src=fontfile='{fontfile}':text='':"
                    f"fontcolor=white:fontsize={fontsize}:"
                    f"shadowx=2:shadowy=2:shadowcolor=black:"
                    f"{pos_xy}[v2]"
                )
                base_v = "[v2]"

        # ── Animacja subskrypcji: overlay z alfą w oknach enable=between().
        # setpts przesuwa timestampy instancji do jej okna; eof_action=pass
        # oddaje czysty obraz po końcu animacji.
        for i, (ts, te) in enumerate(sub_legs):
            shift = f"+{ts:.3f}/TB" if ts > 0 else ""
            fg.append(
                f"[{sub_idxs[i]}:v]scale={main_w}:{main_h}:force_original_aspect_ratio=decrease,"
                f"pad={main_w}:{main_h}:(ow-iw)/2:(oh-ih)/2:color=black@0.0,"
                f"setpts=PTS-STARTPTS{shift}[sb{i}]"
            )
            fg.append(
                f"{base_v}[sb{i}]overlay=0:0:eof_action=pass:format=auto:"
                f"enable='between(t,{ts:.3f},{te:.3f})'[vs{i}]"
            )
            base_v = f"[vs{i}]"

        # Normalizacja formatu po overlay'ach: RGB+alfa suba (CineForm
        # gbrap12le) potrafi wynegocjować format, którego concat/enkoder
        # nie przyjmie — twardy powrót do yuv420p przed dalszym łańcuchem.
        fg.append(f"{base_v}format=yuv420p[vmain]")
        base_v = "[vmain]"

        # ── Audio animacji subskrypcji: dźwięk z .mov miksowany (amix)
        # z materiałem w tych samych oknach czasowych co obraz — adelay
        # przesuwa każdą instancję do jej okna, duration=first przycina
        # wszystko do długości wycinka, normalize=0 nie rusza poziomów.
        if sub_legs and (sub_meta or {}).get("has_audio"):
            mix_ins = [base_a]
            for i, (ts, _te) in enumerate(sub_legs):
                fg.append(f"[{sub_idxs[i]}:a]asetpts=PTS-STARTPTS,{afmt},"
                          f"adelay={int(ts * 1000)}:all=1[sba{i}]")
                mix_ins.append(f"[sba{i}]")
            fg.append("".join(mix_ins)
                      + f"amix=inputs={len(mix_ins)}:duration=first:normalize=0[asub]")
            base_a = "[asub]"

        out_limit: list[str] = []
        # -shortest: dobre ogólne zabezpieczenie przeciw resztkowemu driftowi
        # klatek (patrz uzasadnienie przy konstrukcji cmd niżej).
        use_shortest = True
        if outro:
            outro_a_src = (f"[{outro_silence_idx}:a]" if outro_silence_idx >= 0
                           else f"[{outro_idx}:a]")
            # ── Outro = ZAWSZE warstwa compositingu (overlay z alfą), nigdy
            # doklejenie concat'em. Timing z formuły outro_start = dur-od+delay
            # (patrz komentarz przy wyliczeniu na górze): delay=0 → całe outro
            # nałożone na końcówkę materiału, delay=od → czyste doklejenie.
            # Maska alfa na początku .mov przepuszcza główny materiał pod
            # spodem; NIE xfade/crossfade (rozmywa zamiast komponować).
            #
            # Struktura (wzorzec identyczny ze sprawdzonym overlayem SUB):
            #   1. [main] (+ czarna dokładka długości ogona, gdy delay > 0)
            #      --concat--> [vbase]   (tpad milczkiem nie dokleja klatek
            #      na -ss/-t inpucie — stąd concat z lavfi color)
            #   2. [outro] setpts +outro_start, pad color=black@0.0,
            #      format=yuva420p — jawna konwersja do formatu z kanałem
            #      alfa PRZED overlay'em (źródło bez alfy dostaje alfę=1,
            #      źródło z alfą zachowuje maskę; nigdy czarne tło).
            #   3. overlay shortest=0 + eof_action=pass +
            #      enable='gte(t,outro_start)' (enable chroni przed framesync
            #      duplikującym pierwszą klatkę outro od t=0)
            #   4. format=yuv420p dopiero PO overlay'u.
            if black_idx >= 0:
                fg.append(f"{base_v}fps={fps_s}[vmaincfr]")
                fg.append(f"[{black_idx}:v]setsar=1,format=yuv420p[vblack]")
                fg.append(f"[vmaincfr][vblack]concat=n=2:v=1:a=0[vbase]")
                vbase = "[vbase]"
            else:
                vbase = base_v
            fg.append(
                f"[{outro_idx}:v]scale={main_w}:{main_h}:force_original_aspect_ratio=decrease,"
                f"pad={main_w}:{main_h}:(ow-iw)/2:(oh-ih)/2:color=black@0.0,"
                f"setsar=1,format=yuva420p,setpts=PTS-STARTPTS+{outro_start:.3f}/TB[voutro]"
            )
            fg.append(
                f"{vbase}[voutro]overlay=0:0:shortest=0:eof_action=pass:format=auto:"
                f"enable='gte(t,{outro_start:.3f})'[vov]"
            )
            fg.append("[vov]format=yuv420p[vfinal]")
            # Audio: oryginalna ścieżka materiału jest błyskawicznie wyciszana
            # krzywą 0.5 s TUŻ PRZED wejściem outro (afade=out kończy się
            # dokładnie w outro_start) — dźwięk tła znika zanim ścieżka
            # tyłówki wejdzie na pełnym poziomie. Audio outro wjeżdża z
            # opóźnieniem outro_start (adelay); duration=longest domyka
            # całość na T = max(dur, outro_start + od).
            fade_st = max(0.0, outro_start - 0.5)
            fg.append(f"{base_a}afade=t=out:st={fade_st:.3f}:d=0.5[amfade]")
            fg.append(f"{outro_a_src}asetpts=PTS-STARTPTS,{afmt},"
                      f"adelay={int(outro_start * 1000)}:all=1[aoutro]")
            fg.append(f"[amfade][aoutro]amix=inputs=2:duration=longest:normalize=0[afinal]")
            v_map, a_map = "[vfinal]", "[afinal]"
        else:
            v_map, a_map = base_v, base_a

        cmd = [get_ffmpeg(), "-hide_banner", "-y"] + inputs + [
            "-filter_complex", ";".join(fg),
            "-map", v_map, "-map", a_map,
            # -fps_mode cfr wymusza stałą liczbę klatek/s w muxerze — dobre
            # ogólne zabezpieczenie, choć samo w sobie NIE gwarantuje że
            # wideo i audio skończą się w tym samym momencie (patrz -shortest
            # niżej — to on faktycznie zamyka lukę).
            "-fps_mode", "cfr", "-r", fps_s,
            "-c:v", codec, *codec_args,
            "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
            *out_limit,
            # -shortest: przy łączeniu z materiałem o innym natywnym
            # fps/kontenerze (typowe dla custom outro .mov) strumień wideo
            # bywa o ułamek klatki KRÓTSZY niż audio — rounding przy -ss/-t
            # trimie wejścia jest frame-based, a audio sample-based, więc
            # drobny drift jest niemal nieunikniony. Bez -shortest odtwarzacz
            # gra audio do końca podczas gdy wideo track już się skończył →
            # user widzi "dźwięk leci, obraz czarny/zamrożony". -shortest
            # kończy OBA strumienie w momencie gdy pierwszy z nich się kończy.
            *(["-shortest"] if use_shortest else []),
            "-movflags", "+faststart",
            job.output_path,
        ]
        return cmd

    @staticmethod
    def _overlap_value(job: CutterJob, main_dur: float, outro_dur: float) -> float:
        """Efektywny overlap: clamp do [0, 3] i nie dłuższy niż wycinek/outro."""
        ov = min(3.0, max(0.0, float(job.outro_overlap or 0.0)))
        return min(ov, main_dur, outro_dur)

    # ── Job lifecycle ───────────────────────────────────────────────────

    async def start(self, job: CutterJob) -> str:
        """Uruchamia asyncio task ffmpeg. Zwraca "copy" lub "branded"."""
        # Respect output_path już ustawionego z zewnątrz (user wybrał SaveAs)
        if not job.output_path:
            job.output_path = self._output_path(job)
        self.jobs[job.request_id] = job
        total_dur = max(1.0, job.end_ts - job.start_ts)
        # Wybór binarki: PATH-owy ffmpeg bywa bez drawtext (brew bez
        # libfreetype — tekst źródła "nie wyświetlał się", bo filtr nie
        # istniał). Render z tekstem przechodzi wtedy na bundlowaną
        # statyczną binarkę imageio-ffmpeg; dopiero gdy i jej nie ma,
        # degradujemy do renderu bez napisu.
        ffmpeg_bin = get_ffmpeg()
        force_sw = False
        if job.src_text.strip() and not await self._has_filter("drawtext"):
            alt = _bundled_ffmpeg()
            if alt and await self._has_filter("drawtext", alt):
                ffmpeg_bin = alt
                hw_codec, _ = _hw_encoder()
                if hw_codec != "libx264" and not await self._has_encoder(hw_codec, alt):
                    force_sw = True
                logger.info("cutter[%s]: PATH ffmpeg bez drawtext — bundlowana "
                            "binarka %s%s", job.request_id[:8],
                            os.path.basename(alt),
                            " (sw-encode)" if force_sw else "")
            else:
                logger.warning("cutter[%s]: brak drawtext w dostępnych binarkach — "
                               "pomijam tekst źródła %r",
                               job.request_id[:8], job.src_text)
                job.src_text = ""
        if self._has_branding(job):
            meta = await self._probe_media(job.input_path)
            outro = self._resolve_outro(job)
            outro_meta = await self._probe_media(outro) if outro else None
            sub_path = self._resolve_sub(job)
            sub_meta = await self._probe_media(sub_path) if sub_path else None
            if outro_meta:
                # Outro wydłuża output o ogon wystający za koniec materiału
                # (formuła compositingu: outro_start = dur - od + delay,
                # tail = delay dla typowych długości) — bez tego progress
                # bar zatrzymałby się przed 100% do końca renderu.
                od = max(0.1, float(outro_meta.get("duration") or 5.0))
                ov = self._overlap_value(job, total_dur, od)
                o_start = max(0.0, total_dur - od + ov)
                total_dur += max(0.0, (o_start + od) - total_dur)
            cmds = [self._cmd_branded(job, meta, outro_meta, sub_meta, force_sw)]
            if not force_sw:
                # Enkoder GPU (nvenc/videotoolbox) bywa WYPISANY przez
                # `ffmpeg -encoders` jako dostępny, ale zawodzi dopiero w
                # runtime (np. nvenc → exit code -40 "Function not
                # implemented", gdy sterowniki/GPU faktycznie nie wspierają
                # enkodowania). `_run()` niżej próbuje kolejne cmd z listy aż
                # któreś zwróci 0 — dorzucamy identyczny render wymuszony na
                # libx264 jako automatyczny fallback, ten sam mechanizm co
                # istniejący copy→reencode dla trybu supersonicznego.
                cmds.append(self._cmd_branded(job, meta, outro_meta, sub_meta, force_sw=True))
            mode = "branded"
        else:
            # Copy first; pełny re-encode jako fallback (kontener/keyframe fail).
            cmds = [self._cmd_copy(job), self._cmd_reencode(job)]
            mode = "copy"
        # Wszystkie komendy joba lecą na wybranej binarce (buildery zawsze
        # wstawiają "ffmpeg" na argv[0] — tu podmieniamy na picker-a).
        for c in cmds:
            c[0] = ffmpeg_bin
        logger.info("cutter[%s] mode=%s: %s", job.request_id[:8], mode, " ".join(cmds[0]))
        asyncio.create_task(self._run(job, cmds, total_dur))
        return mode

    async def _emit(self, job: CutterJob, **fields) -> None:
        """Prześlij update do DownloadManager (jeśli podpięty). Bezpieczne no-op
        gdy manager=None lub download_task_id pusty."""
        if not self._manager or not job.download_task_id:
            return
        try:
            await self._manager.update_task(job.download_task_id, **fields)
        except Exception as exc:
            logger.debug("cutter[%s] emit fail: %s", job.request_id[:8], exc)

    async def _run_one(self, job: CutterJob, cmd: list[str], total_dur: float) -> int:
        """Pojedyncze odpalenie ffmpeg + parsowanie stderr (progress/speed).
        Zwraca kod wyjścia procesu. Emit do UI co 1%."""
        re_time = re.compile(rb"time=(\d+):(\d+):(\d+)\.(\d+)")
        re_speed = re.compile(rb"speed=\s*([\d.]+x)")
        last_emitted = -1
        stderr_tail: list[bytes] = []
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
            # Windows: CREATE_NEW_PROCESS_GROUP (pozwala send_signal(CTRL_BREAK_EVENT))
            # OR'owane z CREATE_NO_WINDOW (subprocess_flags) — bez tego drugiego
            # render migał czarnym oknem konsoli ffmpeg co każde uruchomienie.
            creationflags=(0x00000200 | subprocess_flags()) if sys.platform == "win32" else 0,
        )
        job.proc = proc
        assert proc.stderr
        async for line in proc.stderr:
            stderr_tail.append(line[:400])
            if len(stderr_tail) > 12:
                stderr_tail.pop(0)
            m = re_time.search(line)
            if m:
                cur = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3))
                job.progress = min(99, int(cur / total_dur * 100))
            s = re_speed.search(line)
            if s:
                job.speed = s.group(1).decode("ascii", errors="replace")
            # Rate-limit UI: emit tylko gdy progress zmienił się (co 1%)
            if job.progress != last_emitted:
                last_emitted = job.progress
                await self._emit(job, progress=float(job.progress), speed_str=job.speed)
        rc = await proc.wait()
        if rc != 0:
            tail = b"\n".join(stderr_tail).decode("utf-8", "replace")
            logger.warning("cutter[%s] ffmpeg exit=%d\n%s", job.request_id[:8], rc, tail)
        return rc

    async def _run(self, job: CutterJob, cmds: list[list[str]], total_dur: float) -> None:
        """Próbuje kolejne komendy z listy aż któraś się powiedzie
        (np. stream-copy → fallback re-encode)."""
        job.status = "rendering"
        try:
            rc = -1
            for i, cmd in enumerate(cmds):
                if i > 0:
                    logger.warning("cutter[%s] poprzednia próba nie powiodła się (exit=%d) — "
                                   "fallback #%d: %s",
                                   job.request_id[:8], rc, i, " ".join(cmd))
                    job.progress = 0
                    job.speed = ""
                    await self._emit(job, progress=0.0, speed_str="")
                rc = await self._run_one(job, cmd, total_dur)
                if rc == 0:
                    break
            if rc == 0:
                job.progress = 100
                job.status = "done"
                logger.info("cutter[%s] done: %s", job.request_id[:8], job.output_path)
                await self._emit(job, status="done", progress=100.0,
                                 output_path=job.output_path, speed_str="")
            else:
                job.status = "error"
                job.error = f"ffmpeg exit {rc}"
                await self._emit(job, status="error", error_msg=job.error)
        except FileNotFoundError:
            job.status = "error"
            job.error = "ffmpeg nie znaleziony w PATH"
            logger.error("cutter[%s]: ffmpeg brak w PATH", job.request_id[:8])
            await self._emit(job, status="error", error_msg=job.error)
        except Exception as exc:
            logger.exception("cutter[%s] crash: %s", job.request_id[:8], exc)
            job.status = "error"
            job.error = str(exc)
            await self._emit(job, status="error", error_msg=job.error)
        finally:
            # Pliki tymczasowe (sendcmd typewritera itp.)
            for p in job.tmp_files:
                try:
                    os.remove(p)
                except OSError:
                    pass
            job.tmp_files.clear()
