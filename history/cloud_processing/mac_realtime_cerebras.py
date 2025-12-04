#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os, sys, json, time, signal, argparse, threading, queue
from collections import deque
from typing import Dict, Any, List
from typing import Tuple
from contextlib import contextmanager

import numpy as np
import sounddevice as sd
from faster_whisper import WhisperModel
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()

# ================== 配置 ==================
ENDPOINT      = os.environ.get("OPENAI_BASE_URL", "https://models.github.ai/inference")
MODEL_NAME    = os.environ.get("OPENAI_MODEL", "openai/gpt-4o-mini")
API_KEY       = os.environ.get("OPENAI_API_KEY", "")

# Whisper
WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "tiny")  # change to "base" if you want to use the larger model
WHISPER_DEV   = os.environ.get("WHISPER_DEV", "cpu")
WHISPER_CT    = os.environ.get("WHISPER_CT", "int8")
LANG_HINT     = "en"  # change to "zh" if you are using Chinese; None if you want to auto-detect language

# 实时音频 & 端点检测（RMS）
SAMPLE_RATE = 16000
CHANNELS    = 1
FRAME_MS    = 20
MAX_SEG_SEC = 20.0          # 单段最长
PRINT_STT_DEFAULT = True

# 纯 Python RMS VAD
RMS_WIN_MS        = 200
RMS_THRESH_DB     = -35.0
RMS_HANG_MS_ON    = 120     # 连续多久的语音才触发"开始"
RMS_HANG_MS_OFF   = 450     # 连续多久的静音才触发"结束"
RMS_LOOKBACK_MS   = 500     # 回溯窗口：检测到语音后往前看多久
RMS_FLOOR_DECAY   = 0.995

# 业务约束
ACTIONS      = {"forward","backward","left","right","stop"}
SPEEDS       = {"L0","L1","L2","L3"}
MAX_DURATION = 30_000     # ms
MAX_COMMANDS = 30         # 一句话内最多多少条

# ================== Structured Outputs（命令数组） ==================
# 顶层只有 "commands" 一个字段（required），服务端要求 additionalProperties=false。
# 每个命令对象所有字段都 required，非适用项使用 null。
CAR_CMDS_SCHEMA = {
    "name": "CarCommands",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {
            "commands": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "action":      { "type": "string", "enum": ["forward","backward","left","right","stop"] },
                        "duration_ms": {
                            "anyOf": [
                                { "type": "integer"},
                                { "type": "null" }
                            ]
                        },
                        "angle_deg": {
                            "anyOf": [
                                { "type": "integer"},
                                { "type": "null" }
                            ]
                        },
                        "speed": {
                            "anyOf": [
                                { "type": "string", "enum": ["L0","L1","L2","L3"] },
                                { "type": "null" }
                            ]
                        }
                    },
                    "required": ["action","duration_ms","angle_deg","speed"],
                    "additionalProperties": False
                }
            }
        },
        "required": ["commands"],
        "additionalProperties": False
    }
}

SYSTEM_PROMPT = (
    "You are a command parser. Extract an ORDERED list of car-driving commands from the user's utterance.\n"
    "Return ONLY a compact JSON object with a single key 'commands', which is an array of objects.\n"
    "Each object must have ALL of these keys: action, duration_ms, angle_deg, speed.\n"
    "Rules per item:\n"
    "- action ∈ ['forward','backward','left','right','stop'].\n"
    "- If action is 'left' or 'right': angle_deg is an integer 1..360, duration_ms is null.\n"
    "- If action is 'forward' or 'backward': duration_ms is an integer 0..30000, angle_deg is null.\n"
    "- If action is 'stop': both duration_ms and angle_deg are null.\n"
    "- speed is optional: set to 'L0'/'L1'/'L2'/'L3' if specified, otherwise null.\n"
    "Do not include any other keys. Preserve the order the user spoke."
)

# ================== 内部计算 ==================
FRAME_SAMPLES   = SAMPLE_RATE * FRAME_MS // 1000
RMS_WIN_FRAMES  = max(1, RMS_WIN_MS // FRAME_MS)
HANG_ON_FRAMES  = max(1, RMS_HANG_MS_ON // FRAME_MS)
HANG_OFF_FRAMES = max(1, RMS_HANG_MS_OFF // FRAME_MS)

stop_event   = threading.Event()
segments_q   = queue.Queue()
whisper_lock = threading.Lock()
segment_counter = 0
counter_lock = threading.Lock()

# per-segment timing helper
@contextmanager
def timed(step: str, timings: list):
    t0 = time.perf_counter()
    try:
        yield
    finally:
        dt_ms = (time.perf_counter() - t0) * 1000.0
        timings.append({"step": step, "ms": round(dt_ms, 2)})

# ================== 工具函数 ==================
def ensure_token() -> str:
    tok = API_KEY
    if not tok:
        print(f"❌ Env {API_KEY} missing.", file=sys.stderr)
        sys.exit(2)
    return tok

def pcm_float_to_dbfs(x: np.ndarray, eps: float = 1e-8) -> float:
    rms = np.sqrt(np.mean(np.square(x), dtype=np.float64) + eps)
    return 20.0 * np.log10(rms + eps)

# ================== LLM 调用 + 强校验（数组版本，零兜底） ==================
def call_model_strict_multi(client: OpenAI, user_text: str) -> Tuple[Dict[str, Any], float]:
    # Returns (validated_data, model_call_ms)
    try:
        t0 = time.perf_counter()
        resp = client.chat.completions.create(
            model=MODEL_NAME,
            messages=[{"role":"system","content":SYSTEM_PROMPT},
                      {"role":"user","content":user_text}],
            temperature=0,
            response_format={"type":"json_schema","json_schema":CAR_CMDS_SCHEMA},
        )
        model_call_ms = (time.perf_counter() - t0) * 1000.0
    except Exception as e:
        raise SystemExit(f"❌ Model call failed: {e}")

    content = resp.choices[0].message.content
    if not content:
        raise SystemExit("❌ Empty model response.")
    try:
        data = json.loads(content)
    except Exception:
        raise SystemExit(f"❌ Non-JSON model response: {content}")
    validated = validate_payload_multi(data)
    return validated, round(model_call_ms, 2)

def validate_payload_multi(data: Dict[str, Any]) -> Dict[str, Any]:
    # 顶层校验
    if list(data.keys()) != ["commands"]:
        raise SystemExit(f"❌ Top-level must contain only 'commands'. Got keys: {sorted(list(data.keys()))}")
    cmds = data["commands"]
    if not isinstance(cmds, list) or not (1 <= len(cmds) <= MAX_COMMANDS):
        raise SystemExit(f"❌ 'commands' must be an array with 1..{MAX_COMMANDS} items.")

    # 逐条校验
    for idx, item in enumerate(cmds):
        if not isinstance(item, dict):
            raise SystemExit(f"❌ commands[{idx}] must be an object.")
        expected = {"action","duration_ms","angle_deg","speed"}
        if set(item.keys()) != expected:
            missing = expected - set(item.keys())
            extra   = set(item.keys()) - expected
            if missing:
                raise SystemExit(f"❌ commands[{idx}] missing keys: {sorted(list(missing))}")
            if extra:
                raise SystemExit(f"❌ commands[{idx}] extra keys: {sorted(list(extra))}")

        action = item["action"]
        if not isinstance(action, str) or action.lower() not in ACTIONS:
            raise SystemExit(f"❌ commands[{idx}] invalid action: {action}")
        action = action.lower()

        duration_ms = item["duration_ms"]
        angle_deg   = item["angle_deg"]
        speed       = item["speed"]

        if action in {"left","right"}:
            if angle_deg is None:
                raise SystemExit(f"❌ commands[{idx}] angle_deg must be integer for turn.")
            if not isinstance(angle_deg, int) or not (1 <= angle_deg <= 360):
                raise SystemExit(f"❌ commands[{idx}] invalid angle_deg: {angle_deg}")
            if duration_ms is not None:
                raise SystemExit(f"❌ commands[{idx}] duration_ms must be null for turn.")
        elif action in {"forward","backward"}:
            if duration_ms is None:
                raise SystemExit(f"❌ commands[{idx}] duration_ms must be integer for straight move.")
            if not isinstance(duration_ms, int) or not (0 <= duration_ms <= MAX_DURATION):
                raise SystemExit(f"❌ commands[{idx}] invalid duration_ms: {duration_ms}")
            if angle_deg is not None:
                raise SystemExit(f"❌ commands[{idx}] angle_deg must be null for straight move.")
        else:  # stop
            if duration_ms is not None or angle_deg is not None:
                raise SystemExit(f"❌ commands[{idx}] stop must have duration_ms=null and angle_deg=null.")

        if speed is not None:
            if not isinstance(speed, str) or speed not in SPEEDS:
                raise SystemExit(f"❌ commands[{idx}] invalid speed: {speed}")

        # 标准化
        item["action"] = action

    return data

# ================== 实时音频：RMS 端点检测 ==================
def audio_producer(device_idx, print_stt=False):
    # 设备健壮化：若未指定，选第一个有输入通道的设备
    if device_idx is None:
        try:
            default_in = sd.default.device[0]
        except Exception:
            default_in = None
        devs = sd.query_devices()
        if default_in is None or devs[default_in]['max_input_channels'] < 1:
            alt = next((i for i,d in enumerate(devs) if d['max_input_channels']>=1), None)
            if alt is None:
                raise SystemExit("❌ No input-capable audio device found.")
            print(f"ℹ️  Selected input device #{alt}: {devs[alt]['name']}")
            device_idx = alt

    ring_sil = deque(maxlen=RMS_HANG_MS_OFF // FRAME_MS)
    ring_vox = deque(maxlen=RMS_HANG_MS_ON // FRAME_MS)
    # 保留最近的音频帧，用于回溯收集（使用独立的回溯窗口）
    # RMS_LOOKBACK_MS (300ms) 可以捕获语音真正的开头，避免丢失第一个音节
    recent_frames = deque(maxlen=RMS_LOOKBACK_MS // FRAME_MS)
    in_speech = False
    seg_frames = []
    seg_start_t = None
    noise_floor_db = -60.0

    def pcm_float_to_db(frame):
        rms = np.sqrt(np.mean(np.square(frame), dtype=np.float64) + 1e-8)
        return 20.0 * np.log10(rms + 1e-8)

    def on_audio(indata, frames, time_info, status):
        nonlocal in_speech, seg_frames, seg_start_t, noise_floor_db
        if stop_event.is_set():
            raise sd.CallbackStop()

        mono = np.asarray(indata[:,0], dtype=np.float32)
        total = len(mono)
        idx = 0
        while idx + FRAME_SAMPLES <= total:
            frame = mono[idx:idx+FRAME_SAMPLES]
            idx += FRAME_SAMPLES

            db = pcm_float_to_db(frame)
            if not in_speech:
                noise_floor_db = max(-80.0, min(-5.0, RMS_FLOOR_DECAY*noise_floor_db + (1.0-RMS_FLOOR_DECAY)*db))

            dynamic_thresh = max(noise_floor_db + 10.0, RMS_THRESH_DB)
            is_voiced = db >= dynamic_thresh

            ring_vox.append(is_voiced)
            ring_sil.append(not is_voiced)
            
            # 始终保留最近的帧（用于回溯）
            if not in_speech:
                recent_frames.append(frame.astype(np.float32))

            if not in_speech and len(ring_vox)==ring_vox.maxlen and sum(ring_vox)==len(ring_vox):
                in_speech = True
                # 回溯收集之前判定为语音的帧
                seg_frames = list(recent_frames)
                # 时间戳也相应往前移
                seg_start_t = time.time() - (len(recent_frames) * FRAME_MS / 1000.0)

            if in_speech:
                seg_frames.append(frame.astype(np.float32))
                too_long = seg_start_t and (time.time() - seg_start_t) >= MAX_SEG_SEC
                long_sil = len(ring_sil)==ring_sil.maxlen and sum(ring_sil)==len(ring_sil)
                if long_sil or too_long:
                    finish_segment(seg_frames, seg_start_t)
                    in_speech = False
                    seg_frames = []
                    seg_start_t = None
                    ring_sil.clear()
                    ring_vox.clear()
                    recent_frames.clear()

    with sd.InputStream(
        samplerate=SAMPLE_RATE,
        device=device_idx,
        channels=CHANNELS,
        dtype="float32",
        blocksize=FRAME_SAMPLES,
        callback=on_audio,
        latency="low"
    ):
        while not stop_event.is_set():
            sd.sleep(50)

def finish_segment(frames_list, seg_start_t=None):
    global segment_counter
    if not frames_list:
        return
    pcm_f32 = np.concatenate(frames_list, axis=0)
    with counter_lock:
        segment_counter += 1
        seg_id = segment_counter
    # compute VAD duration (ms) from detected segment start time to now
    vad_ms = None
    try:
        if seg_start_t is not None:
            vad_ms = max(0.0, (time.time() - seg_start_t) * 1000.0)
    except Exception:
        vad_ms = None
    segments_q.put((seg_id, pcm_f32, vad_ms))

# ================== 消费：Whisper + LLM（数组） ==================
def consumer_worker(print_stt=False):
    token = ensure_token()
    # time OpenAI client init (measured once per consumer)
    t_client = time.perf_counter()
    client = OpenAI(base_url=ENDPOINT, api_key=token)
    client_init_ms = round((time.perf_counter() - t_client) * 1000.0, 2)

    model = None
    while not stop_event.is_set():
        try:
            seg_id, pcm_f32, vad_ms = segments_q.get(timeout=0.1)
        except queue.Empty:
            continue

        # per-segment timings list (ms)
        timings = []

        # record local VAD duration (may be None if unknown)
        if vad_ms is None:
            vad_ms = 0.0
        timings.append({"step": "local_vad", "ms": round(float(vad_ms), 2)})

        # STT: load model (if needed) + transcribe, measured with timed()
        try:
            with whisper_lock:
                if model is None:
                    with timed("whisper_model_load", timings):
                        model = WhisperModel(WHISPER_MODEL, device=WHISPER_DEV, compute_type=WHISPER_CT)
                with timed("whisper_transcribe", timings):
                    segs, _ = model.transcribe(pcm_f32, language=LANG_HINT, without_timestamps=True, vad_filter=False)
                    text = "".join(s.text for s in segs).strip()
        except Exception as e:
            print(f"❌ STT failed (segment #{seg_id}): {e}", file=sys.stderr)
            segments_q.task_done()
            continue

        # print STT text when requested
        if print_stt or PRINT_STT_DEFAULT:
            print(f"👤 STT (segment #{seg_id}): {text or '(empty)'}")

        # If STT results empty, print STT timing summary and continue
        if not text:
            stt_time = sum(x['ms'] for x in timings if x['step'] in ("whisper_model_load","whisper_transcribe")) / 1000.0
            # Build single-line detailed steps string
            if timings:
                step_parts = [f"{i+1}. {rec['step']}: {rec['ms']} ms" for i, rec in enumerate(timings)]
                steps_str = "; ".join(step_parts) + f"; 共 {len(timings)} 步"
                print(f"⏱️  Timing (segment #{seg_id}): STT={stt_time:.3f}s ({steps_str})", file=sys.stderr)
            else:
                print(f"⏱️  Timing (segment #{seg_id}): STT={stt_time:.3f}s", file=sys.stderr)
            segments_q.task_done()
            continue

        # LLM: measure model call attempt so we can report timings even on validation failure
        t_model_call = time.perf_counter()
        try:
            data, model_call_ms = call_model_strict_multi(client, text)
            model_call_ms = float(model_call_ms)
            model_call_failed = False
        except SystemExit as e:
            model_call_ms = (time.perf_counter() - t_model_call) * 1000.0
            model_call_failed = True
            validation_error = e
            data = None
        except Exception as e:
            model_call_ms = (time.perf_counter() - t_model_call) * 1000.0
            model_call_failed = True
            validation_error = e
            data = None

        # Always append the init and model_call timings for reporting
        timings.append({"step": "init_openai_client", "ms": float(client_init_ms)})
        timings.append({"step": "model_call", "ms": round(model_call_ms, 2)})

        # Compute whisper_transcribe time (prefer the specific step)
        wt_ms = 0.0
        for rec in timings:
            if rec['step'] == 'whisper_transcribe':
                wt_ms = rec['ms']
                break

        local_vad_s = float(vad_ms) / 1000.0
        whisper_transcribe_s = wt_ms / 1000.0
        init_openai_s = client_init_ms / 1000.0
        llm_call_s = model_call_ms / 1000.0
        total_s = local_vad_s + whisper_transcribe_s + init_openai_s + llm_call_s

        # Print single-line detailed timing (stderr) in the requested format, include local VAD
        print(f"⏱️  Timing (segment #{seg_id}): local_vad={local_vad_s:.3f}s, whisper_transcribe={whisper_transcribe_s:.3f}s, init_openai_client = {init_openai_s:.3f}s, LLM_call={llm_call_s:.3f}s, Total={total_s:.3f}s", file=sys.stderr)

        # Now handle success vs failure outputs
        if not model_call_failed:
            # Emit each command as JSON line with segment id
            for cmd in data["commands"]:
                cmd["id"] = seg_id
                print(json.dumps(cmd, ensure_ascii=False, separators=(",",":")))
        else:
            # Print validation or pipeline error message to stderr like before
            if isinstance(validation_error, SystemExit):
                print(f"❌ Command validation failed (segment #{seg_id}): {validation_error}", file=sys.stderr)
            else:
                print(f"❌ Command pipeline error (segment #{seg_id}): {validation_error}", file=sys.stderr)
        segments_q.task_done()

# ================== 主入口 ==================
def main():
    parser = argparse.ArgumentParser(description="macOS realtime voice → STRICT JSON command array")
    parser.add_argument("--device", type=int, default=None, help="sounddevice input device index")
    parser.add_argument("--print-stt", action="store_true", help="print STT text")
    args = parser.parse_args()

    if args.device is None:
        print("ℹ️  Tip: run `python -c 'import sounddevice as sd; print(sd.query_devices())'` to find your mic index.", file=sys.stderr)

    print("🎙️  macOS real-time command mode (multi-command; RMS VAD).")
    print(f"   - Endpoint: {RMS_HANG_MS_OFF} ms silence or {MAX_SEG_SEC} s per segment")
    print("Press Ctrl+C to stop.\n")

    t_cons = threading.Thread(target=consumer_worker, kwargs={"print_stt":args.print_stt}, daemon=True)
    t_cons.start()

    try:
        audio_producer(device_idx=args.device, print_stt=args.print_stt)
    except KeyboardInterrupt:
        pass
    finally:
        stop_event.set()
        t_cons.join(timeout=1.0)

if __name__ == "__main__":
    signal.signal(signal.SIGINT, lambda s,f: stop_event.set())
    signal.signal(signal.SIGTERM, lambda s,f: stop_event.set())
    main()
