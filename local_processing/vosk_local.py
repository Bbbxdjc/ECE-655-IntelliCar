import json, queue, sounddevice as sd
from vosk import Model, KaldiRecognizer
from parse_en import to_structured

model = Model("./vosk_models/en-small")
GRAMMAR = ["forward","backward","turn left","turn right","left","right","stop","halt"]
rec = KaldiRecognizer(model,16000,json.dumps(GRAMMAR))
q=queue.Queue()
def cb(indata,frames,time,status): q.put(bytes(indata))

def recognize_once(timeout_s=3):
    import time
    start=time.time()
    with sd.RawInputStream(samplerate=16000,blocksize=8000,dtype='int16',channels=1,callback=cb):
        while time.time()-start<timeout_s:
            data=q.get()
            if rec.AcceptWaveform(data):
                text=json.loads(rec.Result()).get("text","")
                if text:
                    return to_structured(text)
    return {"task_name":"stop","params":{},"confidence":0.0}


if __name__ == "__main__":
    while True:
        print("Listening for command...")
        task = recognize_once()
        print(task)

