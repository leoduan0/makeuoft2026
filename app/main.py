import json
import math
import os
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Deque, Dict, Optional

from dotenv import load_dotenv
import serial
from serial.tools import list_ports
from PySide6.QtCore import QObject, QThread, Qt, QUrl, Signal, Slot
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtWidgets import (
    QApplication,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QComboBox,
    QLabel,
    QMainWindow,
    QPushButton,
    QProgressBar,
    QVBoxLayout,
    QWidget,
)
from elevenlabs.client import ElevenLabs
from elevenlabs.play import play
import mido

load_dotenv()

ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY")
ELEVENLABS_VOICE_ID = "pNInz6obpgDQGcFmaJgB"  # adam
SERIAL_BAUD = 115200
MIN_CALIBRATION_SAMPLES = 10
MIDI_DEFAULT_PORT = "AeroMix"
MIDI_CHANNEL = 0
MIDI_SEND_INTERVAL = 0.03
MIDI_VOLUME_CC = 7
MIDI_VOLUME_NEUTRAL_CC = 112
MIDI_VOLUME_MIN_CC = 0
MIDI_REVERB_CC = 12
MIDI_TEMPO_CC = 13
MIDI_PAUSE_CC = 14
MIDI_VINTAGE_TOGGLE_CC = 15
THUMB_MEDIAN_WINDOW = 5
THUMB_EMA_ALPHA = 0.3
THUMB_CC_DEADBAND = 1
THUMB_CC_SLEW_PER_SEC = 140.0
INDEX_EFFECT_ACTIVATION_CURL = 0.08
INDEX_EFFECT_MEDIAN_WINDOW = 5
INDEX_EFFECT_EMA_ALPHA = 0.3
INDEX_EFFECT_CC_DEADBAND = 1
INDEX_EFFECT_CC_SLEW_PER_SEC = 160.0
ULTRASONIC_VALID_MIN_CM = 1.0
ULTRASONIC_VALID_MAX_CM = 220.0
ULTRASONIC_PALM_UP_CM = 2.0
ULTRASONIC_PALM_UP_SAMPLES = 3
ULTRASONIC_RECOVER_SAMPLES = 2
ENABLE_ULTRASONIC_PAUSE = False
# Sitting setup (right-hand sensor bouncing off left hand):
# at/above this distance, tempo is neutral; closer than this gradually slows.
TEMPO_NORMAL_DISTANCE_CM = 30.0
TEMPO_NO_CHANGE_BELOW_CM = 3.0
TEMPO_NEUTRAL_CC = 90
TEMPO_SLOW_CC = 10
TEMPO_MEDIAN_WINDOW = 5
TEMPO_EMA_ALPHA = 0.25
TEMPO_CC_DEADBAND = 1
TEMPO_CC_SLEW_PER_SEC = 120.0
MIDDLE_VINTAGE_EXTRA_BEND_RATIO = 0.05
MIDDLE_VINTAGE_EXTRA_BEND_MIN = 20
DEBUG_NOTES_ENABLED = False
CALIBRATION_FILENAME = "calibration.json"
# DJ effect CC mappings (disabled for now)
# MIDI_CC_MAP = {
#     "thumb": 20,
#     "index": 21,
#     "middle": 22,
# }

# Debug note mappings
MIDI_NOTE_MAP = {
    "middle": 38,  # D1 snare
}
NOTE_ON_VELOCITY = 110


def _clamp(value: float, min_value: float, max_value: float) -> float:
    return max(min_value, min(value, max_value))


class MidiOutput:
    def __init__(self, port_name: Optional[str] = None):
        self._port_name = port_name
        self._out: Optional[mido.ports.BaseOutput] = None

    def open(self) -> str:
        if self._port_name:
            self._out = mido.open_output(self._port_name)  # type: ignore[attr-defined]
            return self._port_name

        try:
            self._out = mido.open_output(MIDI_DEFAULT_PORT, virtual=True)  # type: ignore[attr-defined]
            return MIDI_DEFAULT_PORT
        except Exception:
            pass

        ports = mido.get_output_names()  # type: ignore[attr-defined]
        if not ports:
            raise RuntimeError("No MIDI output ports available")
        self._out = mido.open_output(ports[0])  # type: ignore[attr-defined]
        return ports[0]

    def send_cc(self, control: int, value: int, channel: int = MIDI_CHANNEL):
        if not self._out:
            return
        message = mido.Message(
            "control_change",
            control=control,
            value=_clamp(value, 0, 127),
            channel=channel,
        )
        self._out.send(message)

    def send_note(
        self, note: int, velocity: int, is_on: bool, channel: int = MIDI_CHANNEL
    ):
        if not self._out:
            return
        message = mido.Message(
            "note_on" if is_on else "note_off",
            note=note,
            velocity=_clamp(velocity, 0, 127),
            channel=channel,
        )
        self._out.send(message)

    def send_realtime(self, kind: str):
        if not self._out:
            return
        if kind not in ("start", "stop", "continue"):
            return
        message = mido.Message(kind)
        self._out.send(message)

    def close(self):
        if self._out:
            try:
                self._out.close()
            except Exception:
                pass
            self._out = None


@dataclass
class CalibrationStep:
    finger: str
    pose: str
    prompt: str


CALIBRATION_STEPS = [
    CalibrationStep("thumb", "bent", "Now bend your thumb fully."),
    CalibrationStep("thumb", "relaxed", "Now straighten your thumb fully."),
    CalibrationStep("index", "bent", "Now bend your index finger fully."),
    CalibrationStep("index", "relaxed", "Please relax your index finger."),
    CalibrationStep("middle", "bent", "Now bend your middle finger fully."),
    CalibrationStep("middle", "relaxed", "Please relax your middle finger."),
]


class SerialReader(QObject):
    readings = Signal(int, int, int, float)
    status = Signal(str)

    def __init__(self, port: str, baud: int):
        super().__init__()
        self._port = port
        self._baud = baud
        self._running = False
        self._ser: Optional[serial.Serial] = None
        self._last_thumb = 0
        self._last_index = 0
        self._last_middle = 0

    def start(self):
        self._running = True
        try:
            self._ser = serial.Serial(self._port, self._baud, timeout=0.1)
            self.status.emit(f"Connected to {self._port}")
        except Exception as exc:
            self.status.emit(f"Failed to open {self._port}: {exc}")
            self._running = False
            return

        while self._running:
            try:
                if not self._ser:
                    break
                line = self._ser.readline().decode(errors="ignore").strip()
                if not line:
                    continue
                lower = line.lower()

                if "," in line:
                    parts = [part.strip() for part in line.split(",")]
                    if len(parts) < 3:
                        continue
                    thumb = int(float(parts[0]))
                    index = int(float(parts[1]))
                    middle = int(float(parts[2]))
                    distance = float("nan")
                    if len(parts) >= 4:
                        distance = float(parts[3])

                    self._last_thumb = thumb
                    self._last_index = index
                    self._last_middle = middle
                    self.readings.emit(thumb, index, middle, distance)
                    continue

                if "distance" in lower:
                    value_text = line.split(":")[-1].strip()
                    distance = float(value_text)
                    self.readings.emit(
                        self._last_thumb,
                        self._last_index,
                        self._last_middle,
                        distance,
                    )
                    continue

                try:
                    distance = float(line)
                except ValueError:
                    continue
                self.readings.emit(
                    self._last_thumb,
                    self._last_index,
                    self._last_middle,
                    distance,
                )
            except Exception:
                continue

        if self._ser:
            try:
                self._ser.close()
            except Exception:
                pass

    def stop(self):
        self._running = False


class TTSSpeaker(QObject):
    audio_ready = Signal(str)
    status = Signal(str)

    def __init__(self):
        super().__init__()
        self._cache = {}
        self._lock = threading.Lock()
        self._client = ElevenLabs(api_key=ELEVENLABS_API_KEY)

    @Slot(str)
    def speak(self, text: str):
        with self._lock:
            if text in self._cache:
                self.audio_ready.emit(self._cache[text])
                return

        audio = self._client.text_to_speech.stream(
            text=text,
            voice_id=ELEVENLABS_VOICE_ID,
            model_id="eleven_multilingual_v2",
            output_format="mp3_44100_128",
        )
        play(audio)


class MainWindow(QMainWindow):
    tts_request = Signal(str)

    def __init__(self):
        super().__init__()
        self.setWindowTitle("AeroMix")
        self._last_values = (0, 0, 0)
        self._latest_values = {
            "thumb": 0,
            "index": 0,
            "middle": 0,
            "distance": 0,
        }
        self._calibration: Dict[str, Dict[str, int]] = {
            "thumb": {
                "relaxed": 0,
                "bent": 0,
                "threshold_on": 0,
                "threshold_off": 0,
                "bent_greater": 1,
            },
            "index": {
                "relaxed": 0,
                "bent": 0,
                "threshold_on": 0,
                "threshold_off": 0,
                "bent_greater": 1,
            },
            "middle": {
                "relaxed": 0,
                "bent": 0,
                "threshold_on": 0,
                "threshold_off": 0,
                "bent_greater": 1,
            },
        }
        self._step_index = 0
        self._midi_out = None
        self._last_midi_send = 0.0
        self._last_thumb_cc: Optional[int] = None
        self._thumb_cc_ema: Optional[float] = None
        self._thumb_cc_slew: Optional[float] = None
        self._thumb_cc_history: Deque[int] = deque(maxlen=THUMB_MEDIAN_WINDOW)
        self._last_echo_cc: Optional[int] = None
        self._echo_cc_ema: Optional[float] = None
        self._echo_cc_slew: Optional[float] = None
        self._echo_cc_history: Deque[int] = deque(maxlen=INDEX_EFFECT_MEDIAN_WINDOW)
        self._last_tempo_cc: Optional[int] = None
        self._tempo_cc_ema: Optional[float] = None
        self._tempo_cc_slew: Optional[float] = None
        self._tempo_cc_history: Deque[int] = deque(maxlen=TEMPO_MEDIAN_WINDOW)
        self._transport_paused = False
        self._no_distance_streak = 0
        self._distance_recover_streak = 0
        self._middle_bent_state = False
        self._vintage_enabled = False
        self._note_states: Dict[str, bool] = {}
        self._recent_samples: Dict[str, list[int]] = {
            "thumb": [],
            "index": [],
            "middle": [],
        }
        self._is_calibrated = False

        self._build_ui()
        self._load_calibration()
        self._setup_audio()
        self._setup_tts_worker()
        self._setup_midi()
        self._refresh_serial_ports(select_best=True)
        self._connect_serial_from_selection()

    def _build_ui(self):
        root = QWidget()
        layout = QVBoxLayout(root)

        self.step_label = QLabel("Calibration step: Not started")
        self.step_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.step_label)

        device_group = QGroupBox("Input Device")
        device_layout = QHBoxLayout(device_group)
        self.port_combo = QComboBox()
        self.refresh_button = QPushButton("Refresh")
        self.connect_button = QPushButton("Connect")
        device_layout.addWidget(self.port_combo)
        device_layout.addWidget(self.refresh_button)
        device_layout.addWidget(self.connect_button)
        layout.addWidget(device_group)

        sensor_group = QGroupBox("Live Sensor Readings")
        sensor_layout = QGridLayout(sensor_group)

        self.thumb_bar = QProgressBar()
        self.index_bar = QProgressBar()
        self.middle_bar = QProgressBar()
        for bar in (self.thumb_bar, self.index_bar, self.middle_bar):
            bar.setRange(0, 1023)

        self.distance_bar = QProgressBar()
        self.distance_bar.setRange(0, int(round(ULTRASONIC_VALID_MAX_CM)))
        self.distance_bar.setFormat("%v cm")

        sensor_layout.addWidget(QLabel("Thumb"), 0, 0)
        sensor_layout.addWidget(self.thumb_bar, 0, 1)
        sensor_layout.addWidget(QLabel("Index"), 1, 0)
        sensor_layout.addWidget(self.index_bar, 1, 1)
        sensor_layout.addWidget(QLabel("Middle"), 2, 0)
        sensor_layout.addWidget(self.middle_bar, 2, 1)
        sensor_layout.addWidget(QLabel("Distance"), 3, 0)
        sensor_layout.addWidget(self.distance_bar, 3, 1)

        layout.addWidget(sensor_group)

        button_row = QHBoxLayout()
        self.start_button = QPushButton("Recalibrate")
        self.capture_button = QPushButton("Capture Step")
        self.capture_button.setEnabled(False)
        button_row.addWidget(self.start_button)
        button_row.addWidget(self.capture_button)
        layout.addLayout(button_row)

        self.status_label = QLabel("Status: Idle")
        layout.addWidget(self.status_label)

        self.setCentralWidget(root)

        self.start_button.clicked.connect(self._start_calibration)
        self.capture_button.clicked.connect(self._capture_step)
        self.refresh_button.clicked.connect(self._refresh_serial_ports)
        self.connect_button.clicked.connect(self._connect_serial_from_selection)

    def _calibration_path(self) -> str:
        return os.path.join(os.path.dirname(__file__), CALIBRATION_FILENAME)

    def _default_finger_calibration(self) -> Dict[str, int]:
        return {
            "relaxed": 0,
            "bent": 0,
            "threshold_on": 0,
            "threshold_off": 0,
            "bent_greater": 1,
        }

    def _is_valid_finger_calibration(self, calib: Dict[str, int]) -> bool:
        relaxed = int(calib.get("relaxed", 0))
        bent = int(calib.get("bent", 0))
        if not (0 <= relaxed <= 1023 and 0 <= bent <= 1023):
            return False
        return relaxed != bent

    def _load_calibration(self):
        path = self._calibration_path()
        if not os.path.exists(path):
            self._is_calibrated = False
            self.step_label.setText("Calibration required. Click Recalibrate to begin.")
            self._set_status("No calibration file found.")
            return

        try:
            with open(path, "r", encoding="utf-8") as handle:
                raw = json.load(handle)
        except Exception as exc:
            self._is_calibrated = False
            self.step_label.setText("Calibration file invalid. Click Recalibrate.")
            self._set_status(f"Failed to load calibration: {exc}")
            return

        loaded: Dict[str, Dict[str, int]] = {}
        for finger in ("thumb", "index", "middle"):
            item = raw.get(finger, {}) if isinstance(raw, dict) else {}
            finger_data = self._default_finger_calibration()

            # Backward compatibility with older calibration files.
            if "min" in item and "max" in item:
                finger_data["relaxed"] = int(item["min"])
                finger_data["bent"] = int(item["max"])

            for key in (
                "relaxed",
                "bent",
                "threshold_on",
                "threshold_off",
                "bent_greater",
            ):
                if key in item:
                    finger_data[key] = int(item[key])

            self._compute_finger_thresholds(finger, finger_data)

            if not self._is_valid_finger_calibration(finger_data):
                self._is_calibrated = False
                self.step_label.setText("Calibration invalid. Click Recalibrate.")
                self._set_status("Calibration file values are invalid.")
                return

            loaded[finger] = finger_data

        self._calibration = loaded
        self._is_calibrated = True
        self.step_label.setText("Calibration loaded. Click Recalibrate to run again.")
        self._set_status(f"Loaded calibration from {path}")

    def _setup_audio(self):
        self.player = QMediaPlayer(self)
        self.audio_output = QAudioOutput(self)
        self.player.setAudioOutput(self.audio_output)

    def _setup_tts_worker(self):
        self.tts_thread = QThread(self)
        self.tts_worker = TTSSpeaker()
        self.tts_worker.moveToThread(self.tts_thread)
        self.tts_thread.start()
        self.tts_worker.audio_ready.connect(self._play_audio)
        self.tts_worker.status.connect(self._set_status)
        self.tts_request.connect(
            self.tts_worker.speak, Qt.ConnectionType.QueuedConnection
        )

    def _setup_midi(self):
        port_name = os.getenv("MIDI_PORT")
        self._midi_out = MidiOutput(port_name=port_name)
        try:
            opened_name = self._midi_out.open()
            self._send_midi_panic()
            self._set_status(f"MIDI ready: {opened_name}")
        except Exception as exc:
            self._midi_out = None
            self._set_status(f"MIDI unavailable: {exc}")

    def _send_midi_panic(self):
        if not self._midi_out:
            return
        # Stop any lingering notes from previous mappings/runs.
        self._midi_out.send_cc(123, 0)  # all notes off
        self._midi_out.send_cc(120, 0)  # all sound off
        for note in (36, 42):
            self._midi_out.send_note(note, 0, False)

    def _refresh_serial_ports(self, select_best: bool = False):
        ports = list(list_ports.comports())
        self.port_combo.blockSignals(True)
        self.port_combo.clear()

        if not ports:
            self.port_combo.addItem("No ports found", None)
            self.port_combo.setEnabled(False)
            self.connect_button.setEnabled(False)
            self._set_status("No serial ports found.")
            self.port_combo.blockSignals(False)
            return

        self.port_combo.setEnabled(True)
        self.connect_button.setEnabled(True)
        for port in ports:
            label = f"{port.device} — {port.description}"
            self.port_combo.addItem(label, port.device)

        if select_best:
            best = self._detect_port(ports)
            if best:
                index = self.port_combo.findData(best)
                if index != -1:
                    self.port_combo.setCurrentIndex(index)
        self.port_combo.blockSignals(False)

    def _connect_serial_from_selection(self):
        port = self.port_combo.currentData()
        if not port:
            self._set_status("No serial port selected.")
            return
        self._connect_serial(port)

    def _connect_serial(self, port: str):
        if hasattr(self, "serial_worker"):
            self.serial_worker.stop()
        if hasattr(self, "serial_thread"):
            self.serial_thread.quit()
            self.serial_thread.wait(1000)

        self.serial_thread = QThread(self)
        self.serial_worker = SerialReader(port, SERIAL_BAUD)
        self.serial_worker.moveToThread(self.serial_thread)
        self.serial_thread.started.connect(self.serial_worker.start)
        self.serial_worker.readings.connect(self._update_readings)
        self.serial_worker.status.connect(self._set_status)
        self.serial_thread.start()

    def _detect_port(self, ports=None):
        if ports is None:
            ports = list(list_ports.comports())
        if not ports:
            return None
        for port in ports:
            if (
                "Arduino" in port.description
                or "ttyACM" in port.device
                or "usbmodem" in port.device
                or "usbserial" in port.device
            ):
                return port.device
        return ports[0].device

    @Slot(int, int, int, float)
    def _update_readings(
        self,
        thumb: int,
        index: int,
        middle: int,
        distance: float,
    ):
        self._last_values = (thumb, index, middle)
        self._recent_samples["thumb"].append(thumb)
        self._recent_samples["index"].append(index)
        self._recent_samples["middle"].append(middle)
        for finger in ("thumb", "index", "middle"):
            if len(self._recent_samples[finger]) > 25:
                self._recent_samples[finger] = self._recent_samples[finger][-25:]

        self._latest_values.update(
            {
                "thumb": thumb,
                "index": index,
                "middle": middle,
                "distance": int(round(distance)) if math.isfinite(distance) else 0,
            }
        )
        self.thumb_bar.setValue(thumb)
        self.index_bar.setValue(index)
        self.middle_bar.setValue(middle)
        if math.isfinite(distance):
            distance_cm = int(round(_clamp(distance, 0.0, ULTRASONIC_VALID_MAX_CM)))
            self.distance_bar.setValue(distance_cm)
            self.distance_bar.setFormat(f"{distance_cm} cm")
        else:
            self.distance_bar.setValue(0)
            self.distance_bar.setFormat("No echo")
        self._send_midi(thumb, index, middle, distance)

    def _compute_finger_thresholds(self, finger: str, calib: Dict[str, int]):
        relaxed = int(calib.get("relaxed", 0))
        bent = int(calib.get("bent", 0))
        center = (relaxed + bent) / 2.0
        span = abs(bent - relaxed)
        hysteresis = max(8, int(round(span * 0.08)))
        bent_greater = 1 if bent >= relaxed else 0

        if bent_greater:
            threshold_on = int(round(center + hysteresis / 2))
            threshold_off = int(round(center - hysteresis / 2))
        else:
            threshold_on = int(round(center - hysteresis / 2))
            threshold_off = int(round(center + hysteresis / 2))

        calib["threshold_on"] = threshold_on
        calib["threshold_off"] = threshold_off
        calib["bent_greater"] = bent_greater

    def _is_finger_bent(self, finger: str, value: int) -> bool:
        calib = self._calibration.get(finger)
        if not calib:
            return False

        is_on = self._note_states.get(finger, False)
        threshold_on = int(calib.get("threshold_on", 0))
        threshold_off = int(calib.get("threshold_off", 0))
        bent_greater = bool(calib.get("bent_greater", 1))

        if bent_greater:
            if is_on:
                return value > threshold_off
            return value >= threshold_on

        if is_on:
            return value < threshold_off
        return value <= threshold_on

    def _send_midi(
        self,
        thumb: int,
        index: int,
        middle: int,
        distance: float,
    ):
        if not self._midi_out:
            return
        if not self._is_calibrated:
            return
        now = time.monotonic()
        elapsed = now - self._last_midi_send
        if elapsed < MIDI_SEND_INTERVAL:
            return
        self._last_midi_send = now

        thumb_cc = self._thumb_volume_cc(thumb, elapsed)
        if thumb_cc is not None and thumb_cc != self._last_thumb_cc:
            self._midi_out.send_cc(MIDI_VOLUME_CC, thumb_cc)
            self._last_thumb_cc = thumb_cc

        echo_cc = self._index_reverb_cc(index, elapsed)
        if echo_cc is not None and echo_cc != self._last_echo_cc:
            self._midi_out.send_cc(MIDI_REVERB_CC, echo_cc)
            self._last_echo_cc = echo_cc

        if ENABLE_ULTRASONIC_PAUSE:
            self._handle_transport_pause(distance)
        tempo_cc = self._ultrasonic_tempo_cc(distance, elapsed)
        if tempo_cc is not None and tempo_cc != self._last_tempo_cc:
            self._midi_out.send_cc(MIDI_TEMPO_CC, tempo_cc)
            self._last_tempo_cc = tempo_cc

        self._handle_middle_vintage_toggle(middle)

        if DEBUG_NOTES_ENABLED:
            self._set_note_state("middle", self._is_finger_bent("middle", middle))

    def _is_bent_with_hysteresis(self, finger: str, value: int, is_on: bool) -> bool:
        calib = self._calibration.get(finger)
        if not calib:
            return False

        threshold_on = int(calib.get("threshold_on", 0))
        threshold_off = int(calib.get("threshold_off", 0))
        bent_greater = bool(calib.get("bent_greater", 1))

        if bent_greater:
            if is_on:
                return value > threshold_off
            return value >= threshold_on

        if is_on:
            return value < threshold_off
        return value <= threshold_on

    def _handle_middle_vintage_toggle(self, middle_value: int):
        if not self._midi_out:
            return

        bent_now = self._is_middle_vintage_bent(middle_value, self._middle_bent_state)
        if bent_now and not self._middle_bent_state:
            self._vintage_enabled = not self._vintage_enabled
            self._midi_out.send_cc(
                MIDI_VINTAGE_TOGGLE_CC,
                127 if self._vintage_enabled else 0,
            )

        self._middle_bent_state = bent_now

    def _is_middle_vintage_bent(self, value: int, is_on: bool) -> bool:
        calib = self._calibration.get("middle")
        if not calib:
            return False

        relaxed = int(calib.get("relaxed", 0))
        bent = int(calib.get("bent", 0))
        bent_greater = bool(calib.get("bent_greater", 1))
        threshold_on = int(calib.get("threshold_on", 0))
        threshold_off = int(calib.get("threshold_off", 0))

        span = abs(bent - relaxed)
        extra = max(
            MIDDLE_VINTAGE_EXTRA_BEND_MIN,
            int(round(span * MIDDLE_VINTAGE_EXTRA_BEND_RATIO)),
        )

        if bent_greater:
            threshold_on += extra
            threshold_off += extra
            if is_on:
                return value > threshold_off
            return value >= threshold_on

        threshold_on -= extra
        threshold_off -= extra
        if is_on:
            return value < threshold_off
        return value <= threshold_on

    def _finger_curl_amount(self, finger: str, finger_value: int) -> Optional[float]:
        calib = self._calibration.get(finger)
        if not calib:
            return None

        straight = int(calib.get("relaxed", 0))
        curled = int(calib.get("bent", 0))
        span = curled - straight
        if span == 0:
            return None

        curl_amount = (finger_value - straight) / span
        return _clamp(curl_amount, 0.0, 1.0)

    def _thumb_volume_cc(self, thumb_value: int, elapsed: float) -> Optional[int]:
        curl_amount = self._finger_curl_amount("thumb", thumb_value)
        if curl_amount is None:
            return None

        raw_cc = int(
            round(
                MIDI_VOLUME_NEUTRAL_CC
                - curl_amount * (MIDI_VOLUME_NEUTRAL_CC - MIDI_VOLUME_MIN_CC)
            )
        )

        self._thumb_cc_history.append(raw_cc)
        median_cc = sorted(self._thumb_cc_history)[len(self._thumb_cc_history) // 2]

        if self._thumb_cc_ema is None:
            self._thumb_cc_ema = float(median_cc)
        else:
            self._thumb_cc_ema = (
                THUMB_EMA_ALPHA * float(median_cc)
                + (1.0 - THUMB_EMA_ALPHA) * self._thumb_cc_ema
            )

        target = self._thumb_cc_ema
        if self._thumb_cc_slew is None:
            self._thumb_cc_slew = target
        else:
            max_delta = THUMB_CC_SLEW_PER_SEC * max(0.0, min(elapsed, 0.2))
            delta = target - self._thumb_cc_slew
            if delta > max_delta:
                delta = max_delta
            elif delta < -max_delta:
                delta = -max_delta
            self._thumb_cc_slew += delta

        cc_value = int(round(_clamp(self._thumb_cc_slew, 0.0, 127.0)))

        if (
            self._last_thumb_cc is not None
            and abs(cc_value - self._last_thumb_cc) < THUMB_CC_DEADBAND
        ):
            return self._last_thumb_cc

        return cc_value

    def _index_reverb_cc(self, index_value: int, elapsed: float) -> Optional[int]:
        index_curl = self._finger_curl_amount("index", index_value)
        if index_curl is None:
            return None

        if index_curl <= INDEX_EFFECT_ACTIVATION_CURL:
            index_strength = 0.0
        else:
            index_strength = (index_curl - INDEX_EFFECT_ACTIVATION_CURL) / (
                1.0 - INDEX_EFFECT_ACTIVATION_CURL
            )

        raw_cc = int(round(_clamp(index_strength, 0.0, 1.0) * 127.0))

        self._echo_cc_history.append(raw_cc)
        median_cc = sorted(self._echo_cc_history)[len(self._echo_cc_history) // 2]

        if self._echo_cc_ema is None:
            self._echo_cc_ema = float(median_cc)
        else:
            self._echo_cc_ema = (
                INDEX_EFFECT_EMA_ALPHA * float(median_cc)
                + (1.0 - INDEX_EFFECT_EMA_ALPHA) * self._echo_cc_ema
            )

        target = self._echo_cc_ema
        if self._echo_cc_slew is None:
            self._echo_cc_slew = target
        else:
            max_delta = INDEX_EFFECT_CC_SLEW_PER_SEC * max(0.0, min(elapsed, 0.2))
            delta = target - self._echo_cc_slew
            if delta > max_delta:
                delta = max_delta
            elif delta < -max_delta:
                delta = -max_delta
            self._echo_cc_slew += delta

        cc_value = int(round(_clamp(self._echo_cc_slew, 0.0, 127.0)))
        if (
            self._last_echo_cc is not None
            and abs(cc_value - self._last_echo_cc) < INDEX_EFFECT_CC_DEADBAND
        ):
            return self._last_echo_cc

        return cc_value

    def _set_transport_paused(self, paused: bool):
        if not self._midi_out:
            return
        if self._transport_paused == paused:
            return

        self._transport_paused = paused
        self._midi_out.send_cc(MIDI_PAUSE_CC, 127 if paused else 0)
        self._midi_out.send_realtime("stop" if paused else "start")

    def _handle_transport_pause(self, distance: float):
        has_distance = math.isfinite(distance) and distance >= ULTRASONIC_VALID_MIN_CM
        palm_up = (not has_distance) or (distance <= ULTRASONIC_PALM_UP_CM)

        if palm_up:
            self._no_distance_streak += 1
            self._distance_recover_streak = 0
        else:
            self._distance_recover_streak += 1
            self._no_distance_streak = 0

        if (
            not self._transport_paused
            and self._no_distance_streak >= ULTRASONIC_PALM_UP_SAMPLES
        ):
            self._set_transport_paused(True)
        elif (
            self._transport_paused
            and self._distance_recover_streak >= ULTRASONIC_RECOVER_SAMPLES
        ):
            self._set_transport_paused(False)

    def _ultrasonic_tempo_cc(self, distance: float, elapsed: float) -> Optional[int]:
        if not math.isfinite(distance):
            return None
        if distance < ULTRASONIC_VALID_MIN_CM or distance > ULTRASONIC_VALID_MAX_CM:
            return None

        # Hand-to-hand baseline: at/above the lower threshold is neutral tempo.
        # Only when the hands move closer than that threshold does slowdown begin.
        slowdown_start = max(
            ULTRASONIC_VALID_MIN_CM,
            TEMPO_NORMAL_DISTANCE_CM - TEMPO_NO_CHANGE_BELOW_CM,
        )
        if distance >= slowdown_start:
            normalized = 0.0
        else:
            span = max(1.0, slowdown_start - ULTRASONIC_VALID_MIN_CM)
            normalized = (slowdown_start - distance) / span

        normalized = _clamp(normalized, 0.0, 1.0)
        raw_cc = int(
            round(TEMPO_NEUTRAL_CC + normalized * (TEMPO_SLOW_CC - TEMPO_NEUTRAL_CC))
        )

        self._tempo_cc_history.append(raw_cc)
        median_cc = sorted(self._tempo_cc_history)[len(self._tempo_cc_history) // 2]

        if self._tempo_cc_ema is None:
            self._tempo_cc_ema = float(median_cc)
        else:
            self._tempo_cc_ema = (
                TEMPO_EMA_ALPHA * float(median_cc)
                + (1.0 - TEMPO_EMA_ALPHA) * self._tempo_cc_ema
            )

        target = self._tempo_cc_ema
        if self._tempo_cc_slew is None:
            self._tempo_cc_slew = target
        else:
            max_delta = TEMPO_CC_SLEW_PER_SEC * max(0.0, min(elapsed, 0.2))
            delta = target - self._tempo_cc_slew
            if delta > max_delta:
                delta = max_delta
            elif delta < -max_delta:
                delta = -max_delta
            self._tempo_cc_slew += delta

        cc_value = int(round(_clamp(self._tempo_cc_slew, 0.0, 127.0)))
        if (
            self._last_tempo_cc is not None
            and abs(cc_value - self._last_tempo_cc) < TEMPO_CC_DEADBAND
        ):
            return self._last_tempo_cc
        return cc_value

    def _start_calibration(self):
        self._is_calibrated = False
        self._calibration = {
            "thumb": self._default_finger_calibration(),
            "index": self._default_finger_calibration(),
            "middle": self._default_finger_calibration(),
        }
        self._last_thumb_cc = None
        self._thumb_cc_ema = None
        self._thumb_cc_slew = None
        self._thumb_cc_history.clear()
        self._last_echo_cc = None
        self._echo_cc_ema = None
        self._echo_cc_slew = None
        self._echo_cc_history.clear()
        self._last_tempo_cc = None
        self._tempo_cc_ema = None
        self._tempo_cc_slew = None
        self._tempo_cc_history.clear()
        self._transport_paused = False
        self._no_distance_streak = 0
        self._distance_recover_streak = 0
        self._middle_bent_state = False
        self._vintage_enabled = False
        self._note_states = {}
        self._step_index = 0
        self.capture_button.setEnabled(True)
        self._advance_step()

    def _set_note_state(self, key: str, is_on: bool):
        if not self._midi_out or not DEBUG_NOTES_ENABLED:
            return
        previous = self._note_states.get(key, False)
        if previous == is_on:
            return
        self._note_states[key] = is_on
        self._midi_out.send_note(
            MIDI_NOTE_MAP[key],
            NOTE_ON_VELOCITY,
            is_on,
        )

    def _capture_step(self):
        if self._step_index >= len(CALIBRATION_STEPS):
            return
        step = CALIBRATION_STEPS[self._step_index]
        samples = self._recent_samples[step.finger]
        if len(samples) < MIN_CALIBRATION_SAMPLES:
            self._set_status(
                f"Hold pose steady for {step.finger} (need {MIN_CALIBRATION_SAMPLES} samples)."
            )
            return
        if samples:
            value = int(round(sum(samples) / len(samples)))
        else:
            value = {
                "thumb": self._last_values[0],
                "index": self._last_values[1],
                "middle": self._last_values[2],
            }[step.finger]

        self._calibration[step.finger][step.pose] = value
        self._step_index += 1
        if self._step_index >= len(CALIBRATION_STEPS):
            self.capture_button.setEnabled(False)
            self._finish_calibration()
        else:
            self._advance_step()

    def _advance_step(self):
        step = CALIBRATION_STEPS[self._step_index]
        self._recent_samples[step.finger] = []
        self.step_label.setText(
            f"Step {self._step_index + 1}/{len(CALIBRATION_STEPS)}: {step.prompt}"
        )
        self._set_status("Waiting for user input...")
        self._speak(step.prompt)

    def _speak(self, text: str):
        self._set_status("Generating voice instruction...")
        self.tts_request.emit(text)

    @Slot(str)
    def _play_audio(self, file_path: str):
        self.player.setSource(QUrl.fromLocalFile(file_path))
        self.player.play()
        self._set_status("Playing instruction...")

    def _finish_calibration(self):
        for finger in ("thumb", "index", "middle"):
            self._compute_finger_thresholds(finger, self._calibration[finger])

        if not all(
            self._is_valid_finger_calibration(self._calibration[finger])
            for finger in ("thumb", "index", "middle")
        ):
            self._is_calibrated = False
            self.step_label.setText("Calibration failed. Click Recalibrate.")
            self._set_status("Calibration failed: finger ranges are invalid.")
            return

        self._is_calibrated = True
        self.step_label.setText("Calibration complete")
        self._set_status("Calibration complete. Saving results...")
        self._save_calibration()
        self._speak("Calibration complete.")

    def _save_calibration(self):
        file_path = self._calibration_path()
        with open(file_path, "w", encoding="utf-8") as handle:
            json.dump(self._calibration, handle, indent=2)
        self._set_status(f"Saved calibration to {file_path}")

    @Slot(str)
    def _set_status(self, message: str):
        self.status_label.setText(f"Status: {message}")

    def closeEvent(self, event):  # type: ignore[override]
        if hasattr(self, "serial_worker"):
            self.serial_worker.stop()
        if hasattr(self, "serial_thread"):
            self.serial_thread.quit()
            self.serial_thread.wait(1000)
        if hasattr(self, "tts_thread"):
            self.tts_thread.quit()
            self.tts_thread.wait(1000)
        if self._midi_out:
            self._midi_out.close()
        super().closeEvent(event)


def main():
    app = QApplication(sys.argv)
    window = MainWindow()
    window.resize(520, 360)
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
