# realtime_text.py
import os, sys, json, shutil, wave, tempfile, subprocess, argparse
import numpy as np
from typing import Dict, Any
from faster_whisper import WhisperModel
from openai import OpenAI

# ================== 配置 ==================
ENDPOINT      = "https://models.github.ai/inference"
MODEL_NAME    = "openai/gpt-4o"

# Whisper 配置
WHISPER_MODEL = "base"       # "tiny"/"base"/"small"/"medium"
WHISPER_DEV   = "cpu"
WHISPER_CT    = "int8"
LANG_HINT     = None         # 设为 "zh" 可固定中文；None 自动检测

# 业务约束
ACTIONS       = {"forward","backward","left","right","stop"}
MAX_DURATION  = 30_000
SPEEDS        = {"L0","L1","L2","L3"}

# --------- 结构化输出 JSON Schema（所有字段 required，非适用项用 null） ---------
# 注意：为兼容你服务端：
# - required 必须包含 properties 的所有键
# - additionalProperties 必须为 false
# - “可选”通过 type union with null 实现；模型需把不适用字段置为 null
CAR_CMD_SCHEMA = {
    "name": "CarCommand",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {
            "action":      { "type": "string", "enum": ["forward","backward","left","right","stop"] },
            "duration_ms": { "type": ["integer","null"], "minimum": 0, "maximum": MAX_DURATION },
            "angle_deg":   { "type": ["integer","null"], "minimum": 1, "maximum": 360 },
            # speed 也做成可空；枚举留给本地校验，避免部分实现对 enum+null 的兼容性问题
            "speed":       { "type": ["string","null"] }
        },
        "required": ["action","duration_ms","angle_deg","speed"],
        "additionalProperties": False
    }
}

SYSTEM_PROMPT = (
    "Extract a driving command from the user's message.\n"
    "Return ONLY a compact JSON object matching the schema.\n"
    "Rules:\n"
    "- action ∈ ['forward','backward','left','right','stop'].\n"
    "- If action is 'left' or 'right': set angle_deg to an integer 1..360, and set duration_ms to null.\n"
    "- If action is 'forward' or 'backward': set duration_ms to an integer 0..30000, and set angle_deg to null.\n"
    "- If action is 'stop': set both duration_ms and angle_deg to null.\n"
    "- speed is optional: set to 'L0'/'L1'/'L2'/'L3' if specified, otherwise set to null.\n"
    "Do not include any other keys."
)

# ============== 工具函数（无兜底） ==============
def ensure_token() -> str:
    tok = "GITHUB_TOKEN"
    if not tok:
        print("❌ Env GITHUB_TOKEN missing.", file=sys.stderr)
        sys.exit(2)
    return tok

def require_ffmpeg():
    if shutil.which("ffmpeg") is None:
        print("❌ ffmpeg not found. Install it first.", file=sys.stderr)
        sys.exit(3)

def m4a_to_wav16_mono(path: str) -> str:
    if not os.path.isfile(path):
        print(f"❌ File not found: {path}", file=sys.stderr); sys.exit(4)
    require_ffmpeg()
    fd, wav_path = tempfile.mkstemp(suffix=".wav"); os.close(fd)
    cmd = ["ffmpeg","-y","-i",path,"-ac","1","-ar","16000","-vn","-acodec","pcm_s16le",wav_path]
    try:
        subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
    except subprocess.CalledProcessError as e:
        print("❌ ffmpeg failed:", e.stderr.decode(errors="ignore"), file=sys.stderr)
        sys.exit(5)
    return wav_path

def read_wav_float32(wav_path: str) -> np.ndarray:
    with wave.open(wav_path,"rb") as wf:
        raw = wf.readframes(wf.getnframes())
        pcm = np.frombuffer(raw, dtype=np.int16).astype(np.float32)/32768.0
    return pcm

def stt_from_file(audio_path: str) -> str:
    path = audio_path
    cleanup = None
    if path.lower().endswith(".m4a"):
        path = m4a_to_wav16_mono(audio_path); cleanup = path
    elif path.lower().endswith(".wav"):
        if not os.path.isfile(path):
            print(f"❌ File not found: {path}", file=sys.stderr); sys.exit(4)
    else:
        print("❌ Only .m4a or .wav supported.", file=sys.stderr); sys.exit(6)

    pcm = read_wav_float32(path)
    model = WhisperModel(WHISPER_MODEL, device=WHISPER_DEV, compute_type=WHISPER_CT)
    segs, _ = model.transcribe(pcm, language=LANG_HINT)
    text = "".join(s.text for s in segs).strip()
    if cleanup:
        try: os.remove(cleanup)
        except: pass

    if not text:
        print("❌ STT got empty text.", file=sys.stderr); sys.exit(7)

    # 不做繁简转换或其他清洗
    return text

# ============== 模型调用（单次 json_schema） ==============
def call_model_strict(client: OpenAI, user_text: str) -> Dict[str, Any]:
    try:
        resp = client.chat.completions.create(
            model=MODEL_NAME,
            messages=[{"role":"system","content":SYSTEM_PROMPT},
                      {"role":"user","content":user_text}],
            temperature=0,
            response_format={"type":"json_schema","json_schema":CAR_CMD_SCHEMA},
        )
    except Exception as e:
        print(f"❌ Model call failed: {e}", file=sys.stderr)
        sys.exit(9)

    content = resp.choices[0].message.content
    if not content:
        print("❌ Empty model response.", file=sys.stderr); sys.exit(10)

    try:
        data = json.loads(content)
    except Exception:
        print(f"❌ Non-JSON model response: {content}", file=sys.stderr)
        sys.exit(11)

    return data

# ============== 本地强校验（零兜底/零推断） ==============
def validate_payload(data: Dict[str, Any]) -> Dict[str, Any]:
    # 1) 键集合与类型检查（所有键都必须存在，因为 schema 已 required 全列）
    keys = {"action","duration_ms","angle_deg","speed"}
    if set(data.keys()) != keys:
        missing = keys - set(data.keys())
        extra = set(data.keys()) - keys
        if missing:
            print(f"❌ Missing keys: {sorted(list(missing))}", file=sys.stderr); sys.exit(12)
        if extra:
            print(f"❌ Extra keys: {sorted(list(extra))}", file=sys.stderr); sys.exit(13)

    action = data["action"]
    if not isinstance(action, str) or action.lower() not in ACTIONS:
        print(f"❌ Invalid action: {action}", file=sys.stderr); sys.exit(14)
    action = action.lower()

    duration_ms = data["duration_ms"]
    angle_deg   = data["angle_deg"]
    speed       = data["speed"]

    # 2) 条件规则（不适用项必须为 None）
    if action in {"left","right"}:
        if angle_deg is None:
            print("❌ angle_deg must be an integer for turn.", file=sys.stderr); sys.exit(15)
        if not isinstance(angle_deg, int) or not (1 <= angle_deg <= 360):
            print(f"❌ Invalid angle_deg: {angle_deg}", file=sys.stderr); sys.exit(16)
        if duration_ms is not None:
            print("❌ duration_ms must be null for turn actions.", file=sys.stderr); sys.exit(17)

    elif action in {"forward","backward"}:
        if duration_ms is None:
            print("❌ duration_ms must be an integer for straight move.", file=sys.stderr); sys.exit(18)
        if not isinstance(duration_ms, int) or not (0 <= duration_ms <= MAX_DURATION):
            print(f"❌ Invalid duration_ms: {duration_ms}", file=sys.stderr); sys.exit(19)
        if angle_deg is not None:
            print("❌ angle_deg must be null for straight moves.", file=sys.stderr); sys.exit(20)

    else:  # stop
        if duration_ms is not None or angle_deg is not None:
            print("❌ For 'stop', duration_ms and angle_deg must both be null.", file=sys.stderr); sys.exit(21)

    # 3) speed（可为 None 或合法枚举）
    if speed is not None:
        if not isinstance(speed, str) or speed not in SPEEDS:
            print(f"❌ Invalid speed: {speed}", file=sys.stderr); sys.exit(22)

    # 返回标准化（把 action 小写化）
    data["action"] = action
    return data

# ============== 高层入口（无兜底） ==============
def run_from_text(user_text: str) -> Dict[str,Any]:
    token = ensure_token()
    client = OpenAI(base_url=ENDPOINT, api_key=token)
    raw = call_model_strict(client, user_text)
    return validate_payload(raw)

def run_from_audio(audio_path: str) -> Dict[str,Any]:
    user_text = stt_from_file(audio_path)
    print(f"👤 STT: {user_text}")
    return run_from_text(user_text)

# ============== CLI ==============
def main():
    parser = argparse.ArgumentParser(description="Speech/NLU → strict structured car command (all-required with nulls)")
    parser.add_argument("--text", help="直接解析文本（不经 STT）")
    parser.add_argument("input", nargs="?", help="音频文件（.m4a/.wav）")
    args = parser.parse_args()

    if args.text:
        data = run_from_text(args.text)
    else:
        if not args.input:
            print("Usage:\n  python realtime_text.py --text \"左转90度\"\n  python realtime_text.py voice.m4a", file=sys.stderr)
            sys.exit(1)
        data = run_from_audio(args.input)

    print(json.dumps(data, ensure_ascii=False, separators=(",",":")))

if __name__ == "__main__":
    main()