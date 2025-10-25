import json, queue, sounddevice as sd
from vosk import Model, KaldiRecognizer

MODEL_PATH = "./vosk_models/en-small"
model = Model(MODEL_PATH)
rec = KaldiRecognizer(model, 16000)
q = queue.Queue()

def callback(indata, frames, time, status):
    q.put(bytes(indata))

with sd.RawInputStream(samplerate=16000, blocksize=8000, dtype='int16',
                       channels=1, callback=callback):
    print("🎙 Speak English command (Ctrl+C to stop)")
    while True:
        data = q.get()
        if rec.AcceptWaveform(data):
            res = json.loads(rec.Result())
            print("Text:", res.get("text", ""))
