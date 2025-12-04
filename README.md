# Voice-Controlled Motor Bridge

Realtime speech-to-motion stack for the ECE-655 autonomous car.  
The Pi listens through Vosk, optionally upgrades difficult segments with Whisper + an LLM, and immediately drives the chassis with closed-loop IMU feedback.

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

Only the files in the root folder are needed for the current pipeline.  
Everything under `history/` documents previous iterations and can be ignored when deploying.

## What the Stack Does

1. `vosk_whisper_llm.py` runs two recognizers in parallel:
   - Grammar-constrained Vosk for fast, offline keyword spotting.
   - Freeform Vosk + optional Whisper re-transcription when the grammar misses anything.
2. Every transcript is normalized by `parse_en.py` or, if needed, sent to a Cerebras/OpenAI-compatible JSON mode LLM that must emit the strict command schema below.
3. `voice_motor_bridge.py` launches the recognizer on boot, parses each JSON line, and executes it immediately on the Pi using GPIO PWM plus an MPU-6050 gyroscope for closed-loop turns.

### Command JSON schema

```json
{
  "action": "forward|backward|left|right|stop",
  "duration_ms": 1500,
  "angle_deg": null,
  "speed": "L0|L1|L2|L3|null",
  "id": 42   // segment id inserted by the recognizer
}
```

- `forward/backward` → `duration_ms` in milliseconds (clamped to 30 000), `angle_deg=null`
- `left/right` → `angle_deg` (1–360), `duration_ms=null`
- `stop` → both `duration_ms` and `angle_deg` null

## Prerequisites

- Python 3.9+ (the Pi uses 64-bit Raspberry Pi OS)
- USB microphone
- Raspberry Pi 4/5 with I²C enabled, dual motor driver (L298N), and MPU-6050 IMU
- Reliable network connection for Whisper downloads and LLM calls
- `ffmpeg` in `PATH` (faster-whisper loads it when exporting audio)

## Setup

1. **Clone & virtual environment**
   ```bash
   cd Project
   python3 -m venv .venv
   source .venv/bin/activate
   pip install --upgrade pip
   pip install -r requirements.txt
   ```

2. **Download a Vosk model (already bundled but can be refreshed)**
   ```bash
   mkdir -p vosk_models
   cd vosk_models
   curl -L -o model.zip https://alphacephei.com/vosk/models/vosk-model-small-en-us-0.15.zip
   unzip model.zip && mv vosk-model-small-en-us-0.15 en-small
   ```

3. **Configure environment variables** by creating a `.env` file:
   ```
   OPENAI_API_KEY=replace-with-your-key
   OPENAI_BASE_URL=https://api.cerebras.ai/v1        # optional, defaults to GitHub Models proxy
   OPENAI_MODEL=llama-3.3-70b                        # any JSON-mode capable model name
   VOSK_MODEL_PATH=./vosk_models/en-small            # override if you store models elsewhere
   WHISPER_MODEL=base                                # or tiny/base.small/...
   WHISPER_DEVICE=cpu                                # set to cuda for Jetson-class boards
   ```
   Never commit real API keys.

## Running the recognizer on a laptop (debug mode)

1. Plug in a microphone and list devices:
   ```bash
   python -m sounddevice
   ```
2. Start realtime recognition:
   ```bash
   python vosk_whisper_llm.py --device <input_index> --model ./vosk_models/en-small
   ```
   - `--debug` prints every transcript plus per-stage latency.
   - `--no-print-stt` hides the plain-text transcript (only JSON remains).
3. Speak phrases such as “forward five seconds then stop”.  
   JSON lines appear in the console, each tagged with a segment id.

## Running the full Pi motor bridge

1. Enable I²C (`raspi-config` → Interfaces).
2. Wire MPU-6050 to SDA/SCL and the motor driver to the pins below:

| GPIO | Usage |
|------|-------|
| 17/18 | Motor A direction |
| 27/22 | Motor B direction |
| 23/24 | Motor PWM enables |

3. Calibrate the IMU and start the loop (requires sudo only if GPIO permissions demand it):
   ```bash
   source .venv/bin/activate
   python voice_motor_bridge.py
   ```
   - The script auto-calibrates the gyro, starts the recognizer subprocess, and streams every valid command into a queue processed by the motor worker.
   - Closed-loop turns ramp speed down, monitor overshoot, and re-tune the guard band automatically.

### Stopping safely

Press `Ctrl+C` once. The script intercepts the signal, terminates the recognizer, flushes the queue, and calls `GPIO.cleanup()`.

## Development Notes

- `parse_en.py` only needs the words listed in `vosk_config.py`. Update both files together whenever adding new grammar.
- `vosk_whisper_llm.py` exposes useful environment knobs:
  - `LLM_MAX_WORKERS` limits concurrent API requests.
  - `WHISPER_MAX_WORKERS` controls Whisper threads.
  - `AUDIO_BLOCK_SIZE` tunes latency (in frames at 16 kHz).
- `history/` contains previous cloud/local stacks, calibration scripts, and experiments. They are not imported by the new runtime but are handy for regression checks.

## Troubleshooting

- **No audio / Vosk crash** → verify the correct `--device` index and that the sampling rate is 16 kHz.
- **LLM errors** → check network connectivity and `OPENAI_API_KEY`. The process aborts if the key is missing.
- **IMU disconnects** → the bridge auto-reconnects, but inspect wiring if you see repeated “IMU 读取失败” logs.
- **Motor overshoot** → watch the `[调参]` messages; the adaptive guard band stores the latest correction.

## Sample Test Commands

Use these phrases to sanity-check the full stack (speech → JSON → motor queue):

- “Go forward for five seconds at speed L2.”
- “Turn right ninety degrees, then stop.”
- “Go backward for three seconds, turn left forty-five degrees, go forward for two seconds.”
- “Make a square.”
- “Repeat forward two seconds and backward two seconds four times.”
- “Drive a five-pointed star shape.”

The recognizer should emit one JSON line per action matching the schema described earlier. When running `voice_motor_bridge.py`, each decoded line is echoed before the motor worker executes it.

## License

Duke University ECE-655 course project.
