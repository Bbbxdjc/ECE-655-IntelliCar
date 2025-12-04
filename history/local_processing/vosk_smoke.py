#!/usr/bin/env python3
"""
Vosk smoke test: basic voice-to-text recognition.
Tests that Vosk model loading and audio input work correctly.
"""

import os
import sys
import json
import queue
import argparse
import sounddevice as sd
from vosk import Model, KaldiRecognizer

# Use environment variable or default path
MODEL_PATH = os.environ.get("VOSK_MODEL_PATH", "./vosk_models/en-small")


def main():
    """Run smoke test."""
    parser = argparse.ArgumentParser(description="Vosk smoke test: basic voice-to-text")
    parser.add_argument("--device", type=int, default=None, help="Input device index")
    parser.add_argument("--list-devices", action="store_true", help="List audio devices and exit")
    args = parser.parse_args()
    
    # List devices if requested
    if args.list_devices:
        print("Available audio devices:")
        print(sd.query_devices())
        return 0
    
    # Print startup information
    print("="*60)
    print("Vosk Smoke Test: Basic Voice-to-Text")
    print("="*60)
    print(f"Model path: {MODEL_PATH}")
    
    # Query and display audio device info
    try:
        devices = sd.query_devices()
        device_idx = args.device
        
        if device_idx is None:
            device_idx = sd.default.device[0]
            device_name = devices[device_idx]['name'] if device_idx is not None else "Unknown"
            print(f"Input device: #{device_idx} - {device_name} (default)")
        else:
            device_name = devices[device_idx]['name']
            print(f"Input device: #{device_idx} - {device_name}")
    except Exception as e:
        print(f"Warning: Could not query audio devices: {e}")
        device_idx = None
    
    print("="*60)
    print("Speak to test recognition. Press Ctrl+C to stop.")
    print()
    
    # Load model
    try:
        model = Model(MODEL_PATH)
        print("Model loaded successfully\n")
    except Exception as e:
        print(f"Error loading model: {e}", file=sys.stderr)
        return 1
    
    rec = KaldiRecognizer(model, 16000)
    q = queue.Queue()
    
    def callback(indata, frames, time, status):
        q.put(bytes(indata))
    
    try:
        with sd.RawInputStream(
            samplerate=16000,
            blocksize=8000,
            dtype='int16',
            channels=1,
            device=device_idx,
            callback=callback
        ):
            while True:
                data = q.get()
                if rec.AcceptWaveform(data):
                    res = json.loads(rec.Result())
                    text = res.get("text", "")
                    if text:
                        print(f"Recognized: {text}")
    except KeyboardInterrupt:
        print("\nStopping...")
        return 0
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
