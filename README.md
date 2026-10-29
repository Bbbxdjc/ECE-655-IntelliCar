# IntelliCar Hybrid Voice-Controlled Rover

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

Duke University ECE 655L course project.

Realtime speech-to-motion stack for the IntelliCar (ECE-655) platform. The Pi listens with Vosk, optionally retries hard segments with Whisper plus a JSON-mode LLM, and drives the chassis with closed-loop IMU feedback.

## Highlights
- Dual-path STT: fast grammar Vosk plus a fallback path (freeform Vosk -> optional Whisper -> LLM JSON parser).
- Strict JSON command schema that the motor bridge consumes immediately.
- Closed-loop turning with the MPU-6050 gyroscope, adaptive stop guard, and PWM ramp-down.
- Works offline for grammar hits; only the fallback path calls external APIs.

## Repository Layout
```
Project/
├── vosk_whisper_llm.py     # Dual-path STT + LLM command extraction
├── voice_motor_bridge.py   # Motor + MPU6050 controller that runs the STT script
├── parse_en.py             # Lightweight grammar-aware parser for Vosk hits
├── vosk_config.py          # Shared grammar tokens
├── vosk_models/            # Bundled Vosk model (en-small) and ZIP
├── history/                # Earlier revisions (kept for reference only)
└── README.md
```
Only the root files are needed for the current pipeline. Everything under `history/` documents previous iterations and is not used at runtime.

## What the Stack Does
1) `vosk_whisper_llm.py` runs two recognizers in parallel: grammar-constrained Vosk for low-latency hits, and freeform Vosk that can be upgraded by Whisper when confidence is low.
2) Each transcript is normalized locally by `parse_en.py` or sent to a Cerebras/OpenAI-compatible JSON-mode LLM that must emit the schema below.
3) `voice_motor_bridge.py` launches the recognizer, reads each JSON line, and executes it on the Pi using GPIO PWM and MPU-6050 closed-loop turning.

### Command JSON Schema
```json
{
  "action": "forward|backward|left|right|stop",
  "duration_ms": 1500,
  "angle_deg": null,
  "speed": "L0|L1|L2|L3|null",
  "id": 42
}
```
- `forward/backward` -> `duration_ms` in ms (clamped to 30000), `angle_deg=null`
- `left/right` -> `angle_deg` in degrees (1-360), `duration_ms=null`
- `stop` -> both `duration_ms` and `angle_deg` are null

## Requirements
- Python 3.9+ on Raspberry Pi OS (64-bit)
- USB microphone
- Raspberry Pi 4/5 with I2C enabled, L298N (dual motor driver), and MPU-6050 IMU
- Network access for Whisper downloads and LLM calls (grammar path is offline)
- `ffmpeg` available in `PATH` (required by faster-whisper exports)

## Setup
1) Create a virtual environment and install deps:
```bash
python -m venv .venv
./.venv/Scripts/activate      # Windows
# source .venv/bin/activate   # Linux/Raspberry Pi
pip install --upgrade pip
pip install -r requirements.txt
```

2) Download or refresh the Vosk model (already bundled):
```bash
mkdir -p vosk_models
cd vosk_models
curl -L -o model.zip https://alphacephei.com/vosk/models/vosk-model-small-en-us-0.15.zip
unzip model.zip && mv vosk-model-small-en-us-0.15 en-small
```

3) Create a `.env` file for runtime configuration:
```
OPENAI_API_KEY=replace-with-your-key
OPENAI_BASE_URL=https://api.cerebras.ai/v1    # optional, defaults to GitHub Models proxy
OPENAI_MODEL=llama-3.3-70b                    # any JSON-mode capable model
VOSK_MODEL_PATH=./vosk_models/en-small        # override if models live elsewhere
WHISPER_MODEL=base                            # tiny/base/base.small/... are also fine
WHISPER_DEVICE=cpu                            # set to cuda for Jetson-class boards
```
Never commit real API keys.

## Laptop Debug Run (STT Only)
1) List audio devices: `python -m sounddevice`
2) Start realtime recognition:
```bash
python vosk_whisper_llm.py --device <input_index> --model ./vosk_models/en-small
```
Flags: `--debug` prints per-stage latency; `--no-print-stt` hides plain text and only shows JSON. Speak phrases like "forward five seconds then stop" and watch JSON lines tagged with a segment id.

## Full Motor Bridge on the Pi
1) Enable I2C (`raspi-config` -> Interfaces).
2) Wire the MPU-6050 and L298N:
| GPIO | Usage |
| ---- | ----- |
| 17/18 | Motor A direction |
| 27/22 | Motor B direction |
| 23/24 | Motor PWM enables |

3) Calibrate and start:
```bash
source .venv/bin/activate
python voice_motor_bridge.py
```
The script auto-calibrates the gyro, launches the recognizer subprocess, and streams validated commands into the motor worker. Closed-loop turns ramp speed down, monitor overshoot, and auto-tune the stop guard.

### Stopping Safely
Press `Ctrl+C` once. The signal handler terminates the recognizer, drains the queue, and calls `GPIO.cleanup()`.

## Development Notes
- `parse_en.py` expects the tokens in `vosk_config.py`; keep them in sync when adding grammar.
- Useful env knobs in `vosk_whisper_llm.py`: `LLM_MAX_WORKERS`, `WHISPER_MAX_WORKERS`, `AUDIO_BLOCK_SIZE` (frames at 16 kHz).
- `history/` holds earlier cloud/local stacks, calibration helpers, and experiments. It is not imported by the current runtime.

## Troubleshooting
- No audio or Vosk crash: verify the `--device` index and 16 kHz sampling.
- LLM errors: check network reachability and `OPENAI_API_KEY`; the process exits if missing.
- IMU disconnects: the bridge auto-reconnects, but inspect wiring if repeated "IMU read failed" logs appear.
- Motor overshoot: watch `[tune]` logs; the adaptive stop guard records the latest correction.

## Sample Test Commands
Use these to sanity-check the full path (speech -> JSON -> motor queue):
- "Go forward for five seconds at speed L2."
- "Turn right ninety degrees, then stop."
- "Go backward for three seconds, turn left forty-five degrees, go forward for two seconds."
- "Make a square."
- "Repeat forward two seconds and backward two seconds four times."
- "Drive a five-pointed star shape."
The recognizer should emit one JSON line per action. With `voice_motor_bridge.py` running, each line is echoed before the motor worker executes it.

## Team

- [Yifan Xu](https://github.com/yxu-dev)
- [Yihang Zou](https://github.com/Bbbxdjc)
- [Yuchu Guo](https://github.com/williamguo34)

## License

This project is licensed under the [MIT License](LICENSE).
