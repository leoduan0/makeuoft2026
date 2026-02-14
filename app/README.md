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
```

3. Run

```
python main.py
```

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

## Output

Calibration results are saved to calibration.json in the working directory.
