# Flex Sensor Calibration (Qt + Arduino + ElevenLabs)

Minimal Qt prototype that reads three flex sensors over serial and guides calibration with ElevenLabs TTS (PySide6).

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

## Ableton MIDI setup (macOS)

Option A: Use the virtual MIDI port created by the app.

1. Open Ableton Live → Settings/Preferences → Link/Tempo/MIDI.
2. In the MIDI Ports list, enable Track/Remote for "AeroMix".
3. Enter MIDI Map Mode (Cmd+M) and map CCs to parameters.

Option B: Use the built-in IAC Bus.

1. Open Audio MIDI Setup → Window → Show MIDI Studio.
2. Double-click IAC Driver and enable it.
3. Restart the app and set: export MIDI_PORT="IAC Driver Bus 1".
4. Enable Track/Remote for that bus in Ableton.
```

## Arduino serial format

Send one line per sample:

```
thumb,index,middle,emaRoll,emaPitch,yaw,avgAx,avgAy,avgAz
```

Example:

```
512,478,501,-2.3,1.1,180.0,0.01,-0.02,0.98
```

Baud rate: 115200

## MIDI mappings (default CCs)

- Thumb → CC 20
- Index → CC 21
- Middle → CC 22
- emaRoll → CC 23
- emaPitch → CC 24
- Yaw → CC 25

## Output

Calibration results are saved to calibration.json in the working directory.
