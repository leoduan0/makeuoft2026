import json
import os
import sys
import threading
import time
from dataclasses import dataclass
from typing import Dict, Optional

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
MIDI_DEFAULT_PORT = "AeroMix"
MIDI_CHANNEL = 0
MIDI_SEND_INTERVAL = 0.03
MIDI_CC_MAP = {
    "thumb": 20,
    "index": 21,
    "middle": 22,
    "roll": 23,
    "pitch": 24,
    "yaw": 25,
}


def _clamp(value: float, min_value: float, max_value: float) -> float:
    return max(min_value, min(value, max_value))


class MidiOutput:
    def __init__(self, port_name: Optional[str] = None):
        self._port_name = port_name
        self._out: Optional[mido.ports.BaseOutput] = None

    def open(self) -> str:
        if self._port_name:
            self._out = mido.open_output(self._port_name)
            return self._port_name

        try:
            self._out = mido.open_output(MIDI_DEFAULT_PORT, virtual=True)
            return MIDI_DEFAULT_PORT
        except Exception:
            pass

        ports = mido.get_output_names()
        if not ports:
            raise RuntimeError("No MIDI output ports available")
        self._out = mido.open_output(ports[0])
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
    mode: str
    prompt: str


CALIBRATION_STEPS = [
    CalibrationStep("thumb", "max", "Now bend your thumb fully."),
    CalibrationStep("thumb", "min", "Please relax your thumb."),
    CalibrationStep("index", "max", "Now bend your index finger fully."),
    CalibrationStep("index", "min", "Please relax your index finger."),
    CalibrationStep("middle", "max", "Now bend your middle finger fully."),
    CalibrationStep("middle", "min", "Please relax your middle finger."),
]


class SerialReader(QObject):
    readings = Signal(int, int, int, float, float, float, float, float, float)
    status = Signal(str)

    def __init__(self, port: str, baud: int):
        super().__init__()
        self._port = port
        self._baud = baud
        self._running = False
        self._ser: Optional[serial.Serial] = None

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
                parts = line.split(",")
                if len(parts) < 9:
                    continue
                thumb = int(float(parts[0]))
                index = int(float(parts[1]))
                middle = int(float(parts[2]))
                ema_roll = float(parts[3])
                ema_pitch = float(parts[4])
                yaw = float(parts[5])
                avg_ax = float(parts[6])
                avg_ay = float(parts[7])
                avg_az = float(parts[8])
                self.readings.emit(
                    thumb,
                    index,
                    middle,
                    ema_roll,
                    ema_pitch,
                    yaw,
                    avg_ax,
                    avg_ay,
                    avg_az,
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
            "emaRoll": 0.0,
            "emaPitch": 0.0,
            "yaw": 0.0,
            "avgAx": 0.0,
            "avgAy": 0.0,
            "avgAz": 0.0,
        }
        self._calibration: Dict[str, Dict[str, int]] = {
            "thumb": {"min": 0, "max": 0},
            "index": {"min": 0, "max": 0},
            "middle": {"min": 0, "max": 0},
        }
        self._step_index = 0
        self._midi_out = None
        self._last_midi_send = 0.0
        self._last_midi_values = {}

        self._build_ui()
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

        sensor_layout.addWidget(QLabel("Thumb"), 0, 0)
        sensor_layout.addWidget(self.thumb_bar, 0, 1)
        sensor_layout.addWidget(QLabel("Index"), 1, 0)
        sensor_layout.addWidget(self.index_bar, 1, 1)
        sensor_layout.addWidget(QLabel("Middle"), 2, 0)
        sensor_layout.addWidget(self.middle_bar, 2, 1)

        layout.addWidget(sensor_group)

        imu_group = QGroupBox("IMU Readings")
        imu_layout = QGridLayout(imu_group)
        self.roll_label = QLabel("0.00")
        self.pitch_label = QLabel("0.00")
        self.yaw_label = QLabel("0.00")
        self.ax_label = QLabel("0.000")
        self.ay_label = QLabel("0.000")
        self.az_label = QLabel("0.000")

        imu_layout.addWidget(QLabel("emaRoll"), 0, 0)
        imu_layout.addWidget(self.roll_label, 0, 1)
        imu_layout.addWidget(QLabel("emaPitch"), 1, 0)
        imu_layout.addWidget(self.pitch_label, 1, 1)
        imu_layout.addWidget(QLabel("Yaw"), 2, 0)
        imu_layout.addWidget(self.yaw_label, 2, 1)
        imu_layout.addWidget(QLabel("avgAx"), 3, 0)
        imu_layout.addWidget(self.ax_label, 3, 1)
        imu_layout.addWidget(QLabel("avgAy"), 4, 0)
        imu_layout.addWidget(self.ay_label, 4, 1)
        imu_layout.addWidget(QLabel("avgAz"), 5, 0)
        imu_layout.addWidget(self.az_label, 5, 1)

        layout.addWidget(imu_group)

        button_row = QHBoxLayout()
        self.start_button = QPushButton("Start Calibration")
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
            self._set_status(f"MIDI ready: {opened_name}")
        except Exception as exc:
            self._midi_out = None
            self._set_status(f"MIDI unavailable: {exc}")

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

    @Slot(int, int, int, float, float, float, float, float, float)
    def _update_readings(
        self,
        thumb: int,
        index: int,
        middle: int,
        ema_roll: float,
        ema_pitch: float,
        yaw: float,
        avg_ax: float,
        avg_ay: float,
        avg_az: float,
    ):
        self._last_values = (thumb, index, middle)
        self._latest_values.update(
            {
                "thumb": thumb,
                "index": index,
                "middle": middle,
                "emaRoll": ema_roll,
                "emaPitch": ema_pitch,
                "yaw": yaw,
                "avgAx": avg_ax,
                "avgAy": avg_ay,
                "avgAz": avg_az,
            }
        )
        self.thumb_bar.setValue(thumb)
        self.index_bar.setValue(index)
        self.middle_bar.setValue(middle)
        self.roll_label.setText(f"{ema_roll:.2f}")
        self.pitch_label.setText(f"{ema_pitch:.2f}")
        self.yaw_label.setText(f"{yaw:.2f}")
        self.ax_label.setText(f"{avg_ax:.3f}")
        self.ay_label.setText(f"{avg_ay:.3f}")
        self.az_label.setText(f"{avg_az:.3f}")
        self._send_midi(thumb, index, middle, ema_roll, ema_pitch, yaw)

    def _normalize_finger(self, finger: str, value: int) -> int:
        calib = self._calibration.get(finger, {"min": 0, "max": 1023})
        min_val = calib.get("min", 0)
        max_val = calib.get("max", 1023)
        if max_val <= min_val:
            min_val = 0
            max_val = 1023
        return int(round(_clamp((value - min_val) / (max_val - min_val), 0, 1) * 127))

    def _normalize_axis(self, value: float, min_val: float, max_val: float) -> int:
        if max_val <= min_val:
            return 0
        normalized = (value - min_val) / (max_val - min_val)
        return int(round(_clamp(normalized, 0, 1) * 127))

    def _send_midi(
        self,
        thumb: int,
        index: int,
        middle: int,
        ema_roll: float,
        ema_pitch: float,
        yaw: float,
    ):
        if not self._midi_out:
            return
        now = time.monotonic()
        if now - self._last_midi_send < MIDI_SEND_INTERVAL:
            return
        self._last_midi_send = now

        values = {
            "thumb": self._normalize_finger("thumb", thumb),
            "index": self._normalize_finger("index", index),
            "middle": self._normalize_finger("middle", middle),
            "roll": self._normalize_axis(ema_roll, -180.0, 180.0),
            "pitch": self._normalize_axis(ema_pitch, -90.0, 90.0),
            "yaw": self._normalize_axis(yaw, -180.0, 180.0),
        }

        for key, midi_value in values.items():
            last_value = self._last_midi_values.get(key)
            if last_value == midi_value:
                continue
            self._last_midi_values[key] = midi_value
            self._midi_out.send_cc(MIDI_CC_MAP[key], midi_value)

    def _start_calibration(self):
        self._step_index = 0
        self.capture_button.setEnabled(True)
        self._advance_step()

    def _capture_step(self):
        if self._step_index >= len(CALIBRATION_STEPS):
            return
        step = CALIBRATION_STEPS[self._step_index]
        value = {
            "thumb": self._last_values[0],
            "index": self._last_values[1],
            "middle": self._last_values[2],
        }[step.finger]
        self._calibration[step.finger][step.mode] = value
        self._step_index += 1
        if self._step_index >= len(CALIBRATION_STEPS):
            self.capture_button.setEnabled(False)
            self._finish_calibration()
        else:
            self._advance_step()

    def _advance_step(self):
        step = CALIBRATION_STEPS[self._step_index]
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
        self.step_label.setText("Calibration complete")
        self._set_status("Calibration complete. Saving results...")
        self._save_calibration()
        self._speak("Calibration complete.")

    def _save_calibration(self):
        file_path = os.path.join(os.getcwd(), "calibration.json")
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
