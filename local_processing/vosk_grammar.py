import json, queue, sounddevice as sd
from vosk import Model, KaldiRecognizer

GRAMMAR = [
  "forward","backward","turn left","turn right","left","right",
  "stop","halt","for","second","seconds","degree","degrees",
  "one","two","three","four","five","six","seven","eight","nine","ten",
  "fifteen","twenty","thirty","forty","forty five","forty-five",
  "sixty","ninety","one hundred","one hundred eighty"
]

model = Model("/Users/$(whoami)/vosk_models/en-small")
rec = KaldiRecognizer(model, 16000, json.dumps(GRAMMAR))
q = queue.Queue()
def cb(indata, frames, time, status): q.put(bytes(indata))

with sd.RawInputStream(samplerate=16000, blocksize=8000, dtype='int16',
                       channels=1, callback=cb):
    print("🎙 Speak car command")
    while True:
        data = q.get()
        if rec.AcceptWaveform(data):
            print(json.loads(rec.Result())["text"])
