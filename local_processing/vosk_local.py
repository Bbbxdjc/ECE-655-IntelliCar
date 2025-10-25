import os
import sys
import json
import queue
import argparse
import sounddevice as sd
from vosk import Model, KaldiRecognizer
from parse_en import to_structured
from vosk_config import GRAMMAR_DEFAULT

# ================== Configuration (env + CLI) ==================
# Env vars (can be overridden by CLI):
# - VOSK_MODEL_PATH: absolute/relative path to model dir (default ./vosk_models/en-small)
# - VOSK_USE_GRAMMAR: 1/0 or true/false (default 1)
# - VOSK_DEVICE_INDEX: integer device index (optional)

DEFAULT_MODEL_PATH = os.environ.get("VOSK_MODEL_PATH", "./vosk_models/en-small")
DEFAULT_USE_GRAMMAR = os.environ.get("VOSK_USE_GRAMMAR", "1").lower() not in {"0", "false", "no"}
ENV_DEVICE_INDEX = os.environ.get("VOSK_DEVICE_INDEX")

# Use shared grammar configuration
GRAMMAR = GRAMMAR_DEFAULT

# Globals initialized in main()
model = None
rec = None
q = queue.Queue()


def cb(indata, frames, time, status):
    """Audio callback function."""
    q.put(bytes(indata))


def recognize_once(timeout_s=3, device_index=None):
    """
    Listen for a single voice command and return structured JSON.
    
    Args:
        timeout_s: Maximum time to listen in seconds
        device_index: Optional sounddevice input device index
    
    Returns:
        Structured command dict with format:
        {
            "action": "forward|backward|left|right|stop",
            "duration_ms": int|null,
            "angle_deg": int|null,
            "speed": str|null
        }
    """
    import time
    
    # Clear queue to avoid stale audio frames from previous calls
    while not q.empty():
        try:
            q.get_nowait()
        except queue.Empty:
            break
    
    # Reset recognizer state to avoid residual effects from previous calls
    global rec
    rec.Reset()
    
    start = time.time()
    
    try:
        with sd.RawInputStream(
            samplerate=16000,
            blocksize=8000,
            dtype='int16',
            channels=1,
            callback=cb,
            device=device_index
        ):
            while time.time() - start < timeout_s:
                try:
                    # Use short timeout to avoid indefinite blocking
                    data = q.get(timeout=0.1)
                except queue.Empty:
                    # No data yet, continue waiting until timeout_s
                    continue
                
                if rec.AcceptWaveform(data):
                    text = json.loads(rec.Result()).get("text", "")
                    if text:
                        return to_structured(text)
    except sd.PortAudioError as e:
        print(f"Audio device error: {e}", file=sys.stderr)
        print("Check microphone connection and permissions.", file=sys.stderr)
        print("List devices: python -c 'import sounddevice as sd; print(sd.query_devices())'", file=sys.stderr)
        return {
            "action": "stop",
            "duration_ms": None,
            "angle_deg": None,
            "speed": None
        }
    except Exception as e:
        print(f"Unexpected error during recognition: {e}", file=sys.stderr)
        return {
            "action": "stop",
            "duration_ms": None,
            "angle_deg": None,
            "speed": None
        }
    
    # Timeout: return stop command
    return {
        "action": "stop",
        "duration_ms": None,
        "angle_deg": None,
        "speed": None
    }

def _init_recognizer(model_path: str, use_grammar: bool):
    """Initialize Vosk model and recognizer with error handling."""
    global model, rec
    try:
        model = Model(model_path)
    except Exception as e:
        print(f"Failed to load Vosk model from: {model_path}", file=sys.stderr)
        print(f"Error: {e}", file=sys.stderr)
        print("Please check:", file=sys.stderr)
        print("  1. Path is correct and model files exist", file=sys.stderr)
        print("  2. Model directory contains: am/, conf/, graph/, ivector/", file=sys.stderr)
        print("  3. Download model: https://alphacephei.com/vosk/models", file=sys.stderr)
        sys.exit(1)
    
    try:
        if use_grammar:
            rec = KaldiRecognizer(model, 16000, json.dumps(GRAMMAR))
        else:
            rec = KaldiRecognizer(model, 16000)
    except Exception as e:
        print(f"Failed to initialize recognizer: {e}", file=sys.stderr)
        sys.exit(1)


def _parse_args():
    parser = argparse.ArgumentParser(description="Vosk local recognizer (unified JSON output)")
    parser.add_argument("--model", default=DEFAULT_MODEL_PATH, help="Path to Vosk model directory")
    parser.add_argument("--device", type=int, default=None, help="Input device index (sounddevice)")
    parser.add_argument("--no-grammar", action="store_true", help="Disable grammar constraints")
    parser.add_argument("--timeout", type=int, default=3, help="Listen timeout per attempt (seconds)")
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()

    # Resolve final config (env provides defaults, CLI overrides)
    model_path = args.model
    use_grammar = not args.no_grammar if args.no_grammar else DEFAULT_USE_GRAMMAR
    if args.device is not None:
        device_index = args.device
    else:
        device_index = int(ENV_DEVICE_INDEX) if ENV_DEVICE_INDEX and ENV_DEVICE_INDEX.isdigit() else None

    print("Starting Vosk local recognizer")
    print(f"  Model: {model_path}")
    print(f"  Grammar: {'ON' if use_grammar else 'OFF'}")
    print(f"  Device: {device_index if device_index is not None else '(default)'}")
    print("Examples: 'forward five seconds', 'turn left ninety degrees', 'stop'\n")

    _init_recognizer(model_path, use_grammar)

    try:
        while True:
            print("Listening for command...")
            task = recognize_once(timeout_s=args.timeout, device_index=device_index)
            
            # Provide clear feedback based on result
            if task['action'] == 'stop' and all(v is None for k, v in task.items() if k != 'action'):
                # All fields null + stop action = timeout/no speech
                print(f"[Timeout] No command recognized in {args.timeout}s")
            else:
                print("[Recognized]")
            
            print(json.dumps(task, ensure_ascii=False, indent=2))
            print()  # Add blank line for readability
    except KeyboardInterrupt:
        print("\nShutting down gracefully...")
        sys.exit(0)

