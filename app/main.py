import json
import os
import sys
import tempfile
import threading
from dataclasses import dataclass
from typing import Dict, Optional

import serial
from serial.tools import list_ports
from PySide6.QtCore import QObject, QThread, Qt, QUrl, Signal, Slot
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtWidgets import (
    QApplication,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QProgressBar,
    QVBoxLayout,
    QWidget,
)
from elevenlabs import VoiceSettings
from elevenlabs.client import ElevenLabs

ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY")
ELEVENLABS_VOICE_ID = "pNInz6obpgDQGcFmaJgB"  # adam
SERIAL_BAUD = 115200


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

    def __init__(self, port: str, baud: int) -> None:
        super().__init__()
        self._port = port
        self._baud = baud
        self._running = False
        self._ser: Optional[serial.Serial] = None

    def start(self) -> None:
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

    def stop(self) -> None:
        self._running = False


class TTSSpeaker(QObject):
    audio_ready = Signal(str)
    status = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self._cache: Dict[str, str] = {}
        self._lock = threading.Lock()
        self._client: ElevenLabs

    @Slot(str)
    def speak(self, text: str) -> None:
        if not ELEVENLABS_API_KEY:
            self.status.emit("Missing ELEVENLABS_API_KEY")
            return
        if self._client is None:
            self._client = ElevenLabs(api_key=ELEVENLABS_API_KEY)
        assert self._client is not None
        with self._lock:
            if text in self._cache:
                self.audio_ready.emit(self._cache[text])
                return
        try:
            response = self._client.text_to_speech.stream(
                voice_id=ELEVENLABS_VOICE_ID,
                output_format="mp3_22050_32",
                text=text,
                model_id="eleven_multilingual_v2",
                voice_settings=VoiceSettings(
                    stability=0.5,
                    similarity_boost=0.75,
                    style=0.0,
                    use_speaker_boost=True,
                    speed=1.0,
                ),
            )
            temp_dir = tempfile.gettempdir()
            file_path = os.path.join(temp_dir, f"elevenlabs_{hash(text)}.mp3")
            with open(file_path, "wb") as handle:
                for chunk in response:
                    if chunk:
                        handle.write(chunk)
            with self._lock:
                self._cache[text] = file_path
            self.audio_ready.emit(file_path)
        except Exception as exc:
            self.status.emit(f"TTS failed: {exc}")


class MainWindow(QMainWindow):
    tts_request = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Flex Sensor Calibration")
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

        self._build_ui()
        self._setup_audio()
        self._setup_tts_worker()
        self._connect_serial()

    def _build_ui(self) -> None:
        root = QWidget()
        layout = QVBoxLayout(root)

        self.step_label = QLabel("Calibration step: Not started")
        self.step_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.step_label)

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

    def _setup_audio(self) -> None:
        self.player = QMediaPlayer(self)
        self.audio_output = QAudioOutput(self)
        self.player.setAudioOutput(self.audio_output)

    def _setup_tts_worker(self) -> None:
        self.tts_thread = QThread(self)
        self.tts_worker = TTSSpeaker()
        self.tts_worker.moveToThread(self.tts_thread)
        self.tts_thread.start()
        self.tts_worker.audio_ready.connect(self._play_audio)
        self.tts_worker.status.connect(self._set_status)
        self.tts_request.connect(
            self.tts_worker.speak, Qt.ConnectionType.QueuedConnection
        )

    def _connect_serial(self) -> None:
        port = self._detect_port()
        if not port:
            self._set_status("No serial port found. Connect Arduino and restart.")
            return
        self.serial_thread = QThread(self)
        self.serial_worker = SerialReader(port, SERIAL_BAUD)
        self.serial_worker.moveToThread(self.serial_thread)
        self.serial_thread.started.connect(self.serial_worker.start)
        self.serial_worker.readings.connect(self._update_readings)
        self.serial_worker.status.connect(self._set_status)
        self.serial_thread.start()

    def _detect_port(self) -> Optional[str]:
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
    ) -> None:
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

    def _start_calibration(self) -> None:
        self._step_index = 0
        self.capture_button.setEnabled(True)
        self._advance_step()

    def _capture_step(self) -> None:
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

    def _advance_step(self) -> None:
        step = CALIBRATION_STEPS[self._step_index]
        self.step_label.setText(
            f"Step {self._step_index + 1}/{len(CALIBRATION_STEPS)}: {step.prompt}"
        )
        self._set_status("Waiting for user input...")
        self._speak(step.prompt)

    def _speak(self, text: str) -> None:
        self._set_status("Generating voice instruction...")
        self.tts_request.emit(text)

    @Slot(str)
    def _play_audio(self, file_path: str) -> None:
        self.player.setSource(QUrl.fromLocalFile(file_path))
        self.player.play()
        self._set_status("Playing instruction...")

    def _finish_calibration(self) -> None:
        self.step_label.setText("Calibration complete")
        self._set_status("Calibration complete. Saving results...")
        self._save_calibration()
        self._speak("Calibration complete.")

    def _save_calibration(self) -> None:
        file_path = os.path.join(os.getcwd(), "calibration.json")
        with open(file_path, "w", encoding="utf-8") as handle:
            json.dump(self._calibration, handle, indent=2)
        self._set_status(f"Saved calibration to {file_path}")

    @Slot(str)
    def _set_status(self, message: str) -> None:
        self.status_label.setText(f"Status: {message}")

    def closeEvent(self, event) -> None:  # type: ignore[override]
        if hasattr(self, "serial_worker"):
            self.serial_worker.stop()
        if hasattr(self, "serial_thread"):
            self.serial_thread.quit()
            self.serial_thread.wait(1000)
        if hasattr(self, "tts_thread"):
            self.tts_thread.quit()
            self.tts_thread.wait(1000)
        super().closeEvent(event)


def main() -> None:
    app = QApplication(sys.argv)
    window = MainWindow()
    window.resize(520, 360)
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
