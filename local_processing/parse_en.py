import re
WORDS2NUM = {
  "one":1,"two":2,"three":3,"four":4,"five":5,"six":6,"seven":7,"eight":8,"nine":9,"ten":10,
  "fifteen":15,"twenty":20,"thirty":30,"forty":40,"forty five":45,"forty-five":45,
  "sixty":60,"ninety":90,"one hundred":100,"one hundred eighty":180
}

def _num(tok):
    t = tok.lower().replace("-", " ")
    if t in WORDS2NUM: return WORDS2NUM[t]
    if t.isdigit(): return int(t)
    return None

def to_structured(text: str):
    t = " ".join(text.lower().split())
    out = {"task_name":"stop","params":{},"source":"local","confidence":0.8}
    if any(k in t for k in ["stop","halt"]): return out
    if "forward" in t or "backward" in t:
        out["task_name"] = "forward" if "forward" in t else "backward"
        m = re.search(r"([\w-]+)\s*(second|seconds|s)?", t)
        dur = _num(m.group(1)) if m else None
        out["params"]["duration_ms"] = int((dur or 2)*1000)
        return out
    if "left" in t:
        out["task_name"] = "turn_left"
        m = re.search(r"([\w-]+)\s*(degree|degrees)?", t)
        ang = _num(m.group(1)) if m else None
        out["params"]["angle_deg"] = int(ang or 90)
        return out
    if "right" in t:
        out["task_name"] = "turn_right"
        m = re.search(r"([\w-]+)\s*(degree|degrees)?", t)
        ang = _num(m.group(1)) if m else None
        out["params"]["angle_deg"] = int(ang or 90)
        return out
    out["confidence"] = 0.3
    return out
