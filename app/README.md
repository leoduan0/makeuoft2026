# Flex Sensor Calibration (Qt + Arduino + ElevenLabs)

Minimal Qt prototype that reads three flex sensors over serial and guides calibration with ElevenLabs TTS (PySide6).

## Setup

1. Install dependencies

```
pip install -r requirements.txt
```

# Flex Sensor Calibration (Qt + Arduino + ElevenLabs)

Qt app that reads three flex sensors over serial, calibrates bend/relaxed poses, and triggers drum notes over MIDI.

## Setup

1. Install dependencies

```
pip install -r requirements.txt
```

2. Set environment variables

```
export ELEVENLABS_API_KEY=your_key_here
# Optional
export ELEVENLABS_VOICE_ID=21m00Tcm4TlvDq8ikWAM
# Optional: choose a specific MIDI port name
# export MIDI_PORT="IAC Driver Bus 1"
```

3. Run

```
python main.py
```

## Ableton MIDI setup (macOS)

Option A: Use the virtual MIDI port created by the app.

1. Open Ableton Live → Settings/Preferences → Link/Tempo/MIDI.
2. In the MIDI Ports list, enable Track/Remote for "AeroMix".
3. Put a Drum Rack on a MIDI track and monitor incoming notes.

Option B: Use the built-in IAC Bus.

1. Open Audio MIDI Setup → Window → Show MIDI Studio.
2. Double-click IAC Driver and enable it.
3. Restart the app and set: export MIDI_PORT="IAC Driver Bus 1".
4. Enable Track/Remote for that bus in Ableton.

## Arduino serial format

Send one line per sample:

```
thumb,index,middle
```

Example:

```
512,478,501
```

Baud rate: 115200

## MIDI note mappings

- Thumb bend → Note 36 (Kick)
- Index bend → Note 42 (Hi-hat)
- Middle bend → Note 38 (Snare)

## Calibration persistence

- Calibration is stored in app/calibration.json.
- If the file exists at startup, calibration is loaded and skipped.
- Click Recalibrate to run calibration again and overwrite the file.
