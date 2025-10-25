# ECE-655-IntelliCar

Voice-controlled Raspberry Pi car with dual-path processing architecture for natural language command recognition.

## Project Overview

This project implements a voice-controlled robotic car using Raspberry Pi, featuring two parallel speech recognition pathways:

- **Local Processing**: Fast, offline recognition using Vosk for simple commands
- **Cloud Processing**: Advanced NLU using faster-whisper + GitHub Models API for complex instructions

## Architecture

### Dual-Path Processing

```
Voice Input
    |
    +---> Local Path (Vosk)  ---------> Simple Commands (fast, offline)
    |
    +---> Cloud Path (Whisper + LLM) -> Complex Commands (powerful, online)
    |
    v
Unified JSON Format
    |
    v
Motor Controller
```

### Unified Command Format

Both processing paths output commands in a standardized JSON format:

```json
{
  "action": "forward|backward|left|right|stop",
  "duration_ms": 5000,
  "angle_deg": null,
  "speed": "L2"
}
```

**Field Specifications:**
- `action` (required): The action type
  - `forward`, `backward`: Linear movement
  - `left`, `right`: Turning movement
  - `stop`: Stop all movement
- `duration_ms` (int|null): Duration in milliseconds for forward/backward actions (0-30000)
- `angle_deg` (int|null): Angle in degrees for left/right turns (1-360)
- `speed` (string|null): Speed level `L0`, `L1`, `L2`, or `L3` (optional)

**Validation Rules:**
- For `forward`/`backward`: `duration_ms` must be integer, `angle_deg` must be `null`
- For `left`/`right`: `angle_deg` must be integer, `duration_ms` must be `null`
- For `stop`: both `duration_ms` and `angle_deg` must be `null`

## Directory Structure

```
ECE-655-IntelliCar/
├── local_processing/       # Vosk-based offline recognition
│   ├── vosk_local.py       # Main recognizer
│   ├── parse_en.py         # Command parser
│   ├── vosk_grammar.py     # Grammar-constrained recognizer
│   ├── vosk_smoke.py       # Test script
│   ├── vosk_models/        # Speech models
│   └── README.md           # Detailed local setup guide
├── cloud_processing/       # Cloud-based NLU
│   ├── realtime_text.py    # Single command parser
│   ├── mac_realtime_multi.py  # Multi-command parser
│   ├── requirements.txt    # Python dependencies
│   └── README.md           # Cloud setup guide
└── README.md               # This file
```

## Quick Start

### Local Processing Setup

```bash
cd local_processing
python3 -m venv venv
source venv/bin/activate
pip install vosk sounddevice numpy

# Download model
mkdir -p vosk_models
cd vosk_models
curl -L -o model.zip https://alphacephei.com/vosk/models/vosk-model-small-en-us-0.15.zip
unzip model.zip && mv vosk-model-small-en-us-0.15 en-small

# Test
cd ..
python3 vosk_local.py
```

### Cloud Processing Setup

```bash
cd cloud_processing
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Configure .env file
echo "GITHUB_TOKEN=your_token_here" > .env

# Test with text
python3 realtime_text.py --text "forward five seconds"

# Test with microphone
python3 mac_realtime_multi.py --print-stt
```

## Example Commands

### Simple Commands (Local Processing)
- "forward ten seconds"
- "backward five seconds"
- "turn left ninety degrees"
- "turn right forty five degrees"
- "stop"

### Complex Commands (Cloud Processing)
- "go forward for 3 seconds, then turn left 90 degrees, then stop"
- "make a square"
- "go zigzag forward five times"
- "drive a 5-pointed star shape"
- "repeat forward 2 seconds and backward 2 seconds four times"

## Example Output

**Command**: "forward five seconds"

**Local Processing Output**:
```json
{
  "action": "forward",
  "duration_ms": 5000,
  "angle_deg": null,
  "speed": null
}
```

**Cloud Processing Output** (same format):
```json
{
  "action": "forward",
  "duration_ms": 5000,
  "angle_deg": null,
  "speed": "L2"
}
```

**Command**: "turn left ninety degrees"

**Output**:
```json
{
  "action": "left",
  "duration_ms": null,
  "angle_deg": 90,
  "speed": null
}
```

## System Requirements

### Development (macOS)
- macOS 11.0 or later
- Python 3.8+
- Homebrew (for dependencies)
- Microphone access

### Deployment (Raspberry Pi)
- Raspberry Pi 4 Model B (4GB RAM recommended) or Raspberry Pi 5
- Raspberry Pi OS (64-bit)
- Python 3.8+
- USB microphone
- Network connectivity (for cloud processing)

## Documentation

- **Local Processing**: See `local_processing/README.md` for detailed Vosk setup
- **Cloud Processing**: See `cloud_processing/README.md` for GitHub Models API setup

## Performance Comparison

| Feature | Local Processing | Cloud Processing |
|---------|------------------|------------------|
| **Latency** | 200-400ms | 1-3 seconds |
| **Accuracy** | Good for simple commands | Excellent for complex commands |
| **Network** | Not required | Required |
| **CPU Usage** | Moderate (40-60%) | Low (5-15%) |
| **Command Types** | Single, simple | Multiple, complex |
| **Use Case** | Real-time control | Planning & sequences |

## Security & Privacy

- **Local Processing**: All processing done on-device, no data transmitted
- **Cloud Processing**: Only transcribed text sent to API, audio processed locally

## License

Duke University ECE-655 Course Project

## Contributors

Developed as part of the ECE-655 course at Duke University.
