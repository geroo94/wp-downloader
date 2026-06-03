"""
LoadingOverlay — Qt-native widok ładowania OSADZONY w głównym oknie aplikacji.

Architektonicznie to dziecko ``MainWindow.centralWidget`` (kontenera ze
``QStackedLayout(StackAll)``) siedzące w stacku NAD ``QWebEngineView``.
Pokrywa cały obszar centrum okna, zasłaniając WebView dopóki interfejs nie
jest gotowy. Gdy aplikacja kończy ładowanie:

  1) ``set_progress(100)`` triggeruje QVariantAnimation chunka czerwony → zielony
     (250 ms, InOutQuad).
  2) ``start_fade_out()`` animuje QGraphicsOpacityEffect tego widgetu z 1.0
     na 0.0 (350 ms, InCubic) i emituje publiczny sygnał ``finished``.
  3) AppController na ``finished`` ukrywa overlay i każe JS w WebView dodać
     klasę ``ready`` do <body> — odpala się CSS transition opacity 0 → 1
     który ujawnia docelowy interfejs.

Wymiary elementów są CELOWO duże (logo 192 px, font 28 pt, pasek 380×6 px)
— wyraźnie większe od typografii i kontrolek głównego UI, żeby ekran
ładowania wyglądał profesjonalnie i odróżniał się od docelowego widoku.

WAŻNE: NIGDY nie ustawiać Qt.WA_TranslucentBackground na MainWindow ani
na CentralStack — łamie compositor surface Chromium na Windowsie (czarne
tło / flicker). Overlay ma jednolite białe tło rysowane normalnie, a fade
działa przez QGraphicsOpacityEffect (na plain QWidget, nie na WebView).
"""

from __future__ import annotations

import logging

from PyQt6.QtCore import (
    QEasingCurve,
    QPropertyAnimation,
    QSize,
    Qt,
    QTimer,
    QVariantAnimation,
    pyqtSignal,
)
from PyQt6.QtGui import QColor, QFont, QPixmap
from PyQt6.QtWidgets import (
    QGraphicsOpacityEffect,
    QLabel,
    QProgressBar,
    QVBoxLayout,
    QWidget,
)

logger = logging.getLogger(__name__)


# Kolory paska — czerwony aplikacyjny → zielony „gotowe"
_BAR_COLOR_START = QColor("#E3000F")
_BAR_COLOR_END = QColor("#22C55E")
_BG_COLOR = "#FBFBFA"


def _bar_stylesheet(color: QColor) -> str:
    """QSS chunka z konkretnym kolorem; ramka i tło bar'a pozostają
    neutralne tak żeby transition QColor nie zostawiał śladów po starym kolorze."""
    return (
        "QProgressBar {"
        " background: #EAEAEA;"
        " border: none;"
        " border-radius: 3px;"
        "}"
        "QProgressBar::chunk {"
        f" background-color: {color.name()};"
        " border-radius: 3px;"
        "}"
    )


class LoadingOverlay(QWidget):
    """Pełnoekranowy overlay w obrębie central widget głównego okna."""

    finished = pyqtSignal()

    _LOGO_SIZE = 192
    _BAR_WIDTH = 380
    _BAR_HEIGHT = 6

    def __init__(self, logo_path: str, parent: QWidget) -> None:
        super().__init__(parent)

        # Jednolite białe tło zakrywa WebView pod spodem.
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(f"LoadingOverlay {{ background-color: {_BG_COLOR}; }}")

        self._build_ui(logo_path)

        # Smoothing: bar dąży do _target_percent, faktycznie wyświetlana
        # wartość to _displayed_percent. Tick 33 ms ~= 30 fps.
        self._target_percent: float = 0.0
        self._displayed_percent: float = 0.0
        self._smoother_timer = QTimer(self)
        self._smoother_timer.setInterval(33)
        self._smoother_timer.timeout.connect(self._smooth_tick)
        self._smoother_timer.start()

        # Animacje jako atrybuty — bez tego GC niszczy je w trakcie.
        self._color_anim: QVariantAnimation | None = None
        self._opacity_effect: QGraphicsOpacityEffect | None = None
        self._fade_anim: QPropertyAnimation | None = None
        self._green_triggered = False

    # ── publiczny API ────────────────────────────────────────────────────

    def set_status(self, text: str) -> None:
        """Aktualizuje linię statusu pod paskiem."""
        self._status_label.setText(text)

    def set_progress(self, percent: int) -> None:
        """Ustawia cel progresu. Pasek smooth-animuje do tej wartości.
        Wartość 100 triggeruje tween koloru chunka czerwony → zielony."""
        percent = max(0, min(100, int(percent)))
        self._target_percent = float(percent)
        if percent >= 100 and not self._green_triggered:
            self._green_triggered = True
            self._start_color_animation()

    def start_fade_out(self, duration_ms: int = 350) -> None:
        """Płynnie wygasza overlay przez QGraphicsOpacityEffect; po zakończeniu
        emituje ``finished``. Plain QWidget + GraphicsEffect = standardowy
        wzorzec Qt, nie dotyka WebView pod spodem."""
        if self._fade_anim is not None and self._fade_anim.state() == QPropertyAnimation.State.Running:
            return  # już animowane
        # GraphicsEffect tworzony raz; jego property ``opacity`` jest animowane.
        if self._opacity_effect is None:
            self._opacity_effect = QGraphicsOpacityEffect(self)
            self._opacity_effect.setOpacity(1.0)
            self.setGraphicsEffect(self._opacity_effect)
        self._fade_anim = QPropertyAnimation(self._opacity_effect, b"opacity", self)
        self._fade_anim.setDuration(int(duration_ms))
        self._fade_anim.setStartValue(self._opacity_effect.opacity())
        self._fade_anim.setEndValue(0.0)
        self._fade_anim.setEasingCurve(QEasingCurve.Type.InCubic)
        self._fade_anim.finished.connect(self._on_fade_finished)
        self._fade_anim.start()

    def force_hide(self) -> None:
        """Awaryjne ukrycie — używane gdy serwer padnie podczas startu.
        Zatrzymuje wszystkie animacje, ukrywa widget. Bez deleteLater —
        AppController zdecyduje kiedy zwolnić."""
        for anim in (self._fade_anim, self._color_anim):
            if anim is not None and anim.state() == QPropertyAnimation.State.Running:
                anim.stop()
        self._smoother_timer.stop()
        self.hide()

    # ── wewnętrzne ───────────────────────────────────────────────────────

    def _build_ui(self, logo_path: str) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(40, 40, 40, 40)
        outer.setSpacing(0)

        # Górny stretch żeby zawartość była wycentrowana pionowo.
        outer.addStretch(1)

        # Logo (duże)
        self._logo_label = QLabel(self)
        pix = QPixmap(logo_path)
        if not pix.isNull():
            scaled = pix.scaled(
                QSize(self._LOGO_SIZE, self._LOGO_SIZE),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            dpr = self.devicePixelRatioF() if hasattr(self, "devicePixelRatioF") else 1.0
            if dpr > 1.0:
                scaled.setDevicePixelRatio(dpr)
            self._logo_label.setPixmap(scaled)
        else:
            logger.warning("LoadingOverlay: logo nie znaleziono: %s", logo_path)
        self._logo_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        outer.addWidget(self._logo_label, alignment=Qt.AlignmentFlag.AlignHCenter)

        outer.addSpacing(20)

        # Nazwa aplikacji (duża)
        self._name_label = QLabel("WP Downloader", self)
        name_font = QFont()
        name_font.setPointSize(28)
        name_font.setWeight(QFont.Weight.Bold)
        name_font.setLetterSpacing(QFont.SpacingType.PercentageSpacing, 95)
        self._name_label.setFont(name_font)
        self._name_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._name_label.setStyleSheet("color: #111;")
        outer.addWidget(self._name_label)

        outer.addSpacing(32)

        # Wrapper żeby pasek miał stałą szerokość 380 px i był wycentrowany.
        bar_wrap = QWidget(self)
        bar_wrap_layout = QVBoxLayout(bar_wrap)
        bar_wrap_layout.setContentsMargins(0, 0, 0, 0)
        bar_wrap_layout.setSpacing(0)
        self._progress = QProgressBar(bar_wrap)
        self._progress.setRange(0, 100)
        self._progress.setValue(0)
        self._progress.setTextVisible(False)
        self._progress.setFixedSize(self._BAR_WIDTH, self._BAR_HEIGHT)
        self._progress.setStyleSheet(_bar_stylesheet(_BAR_COLOR_START))
        bar_wrap_layout.addWidget(self._progress, alignment=Qt.AlignmentFlag.AlignCenter)
        outer.addWidget(bar_wrap, alignment=Qt.AlignmentFlag.AlignHCenter)

        outer.addSpacing(20)

        # Status pod paskiem (większy niż w głównym UI)
        self._status_label = QLabel("Uruchamianie…", self)
        status_font = QFont()
        status_font.setPointSize(14)
        self._status_label.setFont(status_font)
        self._status_label.setStyleSheet("color: #787774;")
        self._status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        outer.addWidget(self._status_label)

        # Dolny stretch domyka wycentrowanie pionowe.
        outer.addStretch(1)

    def _start_color_animation(self) -> None:
        """Tween chunka paska czerwony → zielony (~250 ms)."""
        self._color_anim = QVariantAnimation(self)
        self._color_anim.setDuration(250)
        self._color_anim.setStartValue(_BAR_COLOR_START)
        self._color_anim.setEndValue(_BAR_COLOR_END)
        self._color_anim.setEasingCurve(QEasingCurve.Type.InOutQuad)
        self._color_anim.valueChanged.connect(self._apply_bar_color)
        self._color_anim.start()

    def _apply_bar_color(self, color) -> None:
        if isinstance(color, QColor):
            self._progress.setStyleSheet(_bar_stylesheet(color))

    def _smooth_tick(self) -> None:
        """Co 33 ms zbliża displayed → target. Daje ciągły, gładki ruch
        paska nawet gdy target zmienia się skokowo co setki ms."""
        if abs(self._displayed_percent - self._target_percent) < 0.1:
            if self._displayed_percent != self._target_percent:
                self._displayed_percent = self._target_percent
                self._progress.setValue(int(round(self._displayed_percent)))
            return
        delta = max(0.5, (self._target_percent - self._displayed_percent) * 0.15)
        if self._target_percent > self._displayed_percent:
            self._displayed_percent = min(self._target_percent, self._displayed_percent + delta)
        else:
            self._displayed_percent = max(self._target_percent, self._displayed_percent - delta)
        self._progress.setValue(int(round(self._displayed_percent)))

    def _on_fade_finished(self) -> None:
        self._smoother_timer.stop()
        self.hide()
        # Po hide() emit żeby AppController dorzucił klasę `ready` w body
        # WebView (CSS fade-in interfejsu) i zwolnił overlay przez deleteLater.
        self.finished.emit()
