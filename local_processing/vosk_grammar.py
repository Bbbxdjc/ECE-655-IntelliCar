import os
import sys
import json
import queue
import sounddevice as sd
from vosk import Model, KaldiRecognizer
from vosk_config import GRAMMAR_EXTENDED

# Use environment variable or default path (supports home directory expansion)
MODEL_PATH = os.environ.get("VOSK_MODEL_PATH", 
                            os.path.expanduser("~/vosk_models/en-small"))

# Use shared grammar configuration
GRAMMAR = GRAMMAR_EXTENDED

# Print startup information
print("="*60)
print("Vosk Grammar-Constrained Recognizer Test")
print("="*60)
print(f"Model path: {MODEL_PATH}")
print(f"Grammar size: {len(GRAMMAR)} words")

# Query audio devices
try:
    devices = sd.query_devices()
    default_input = sd.default.device[0]
    if default_input is not None:
        print(f"Input device: #{default_input} - {devices[default_input]['name']}")
except Exception as e:
    print(f"Warning: Could not query audio devices: {e}")

print("="*60)
print("Press Ctrl+C to stop")
print()

try:
    model = Model(MODEL_PATH)
    print("Model loaded successfully")
except Exception as e:
    print(f"Error loading model: {e}", file=sys.stderr)
    sys.exit(1)

rec = KaldiRecognizer(model, 16000, json.dumps(GRAMMAR))
q = queue.Queue()


def cb(indata, frames, time, status):
    q.put(bytes(indata))


try:
    with sd.RawInputStream(samplerate=16000, blocksize=8000, dtype='int16',
                           channels=1, callback=cb):
        while True:
            data = q.get()
            if rec.AcceptWaveform(data):
                result = json.loads(rec.Result())
                text = result.get("text", "")
                if text:
                    print(f"Recognized: {text}")
except KeyboardInterrupt:
    print("\nStopping...")
except Exception as e:
    print(f"Error: {e}", file=sys.stderr)
    sys.exit(1)
