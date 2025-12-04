#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Realtime Vosk STT (e.g. on Raspberry Pi) + Cerebras/OpenAI LLM parsing.

Pipeline:
  - Vosk: streaming speech recognition，自动按语音停顿分段
  - 每个分段的文本 -> LLM（Cerebras / OpenAI-compatible）
  - LLM 返回严格 JSON: {"commands":[{action,duration_ms,angle_deg,speed}, ...]}
  - 每条命令加上 segment id，逐行输出 JSON
"""

import os
import sys
import json
import time
import argparse
import threading
import queue
from collections import deque
from typing import Dict, Any, Tuple, Optional

import numpy as np
import sounddevice as sd
from vosk import Model, KaldiRecognizer
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()

# ================== LLM / Cerebras 配置 ==================
ENDPOINT   = os.environ.get("OPENAI_BASE_URL", "https://models.github.ai/inference")
MODEL_NAME = os.environ.get("OPENAI_MODEL", "openai/gpt-4o-mini")
API_KEY    = os.environ.get("OPENAI_API_KEY", "")

# 业务约束（和 mac_realtime_cerebras.py 保持一致）
ACTIONS      = {"forward", "backward", "left", "right", "stop"}
SPEEDS       = {"L0", "L1", "L2", "L3"}
MAX_DURATION = 30_000  # ms
MAX_COMMANDS = 30      # 一句话最多 N 条命令

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
                        "action":      {"type": "string", "enum": ["forward", "backward", "left", "right", "stop"]},
                        "duration_ms": {"anyOf": [{"type": "integer"}, {"type": "null"}]},
                        "angle_deg":   {"anyOf": [{"type": "integer"}, {"type": "null"}]},
                        "speed":       {"anyOf": [{"type": "string", "enum": ["L0", "L1", "L2", "L3"]}, {"type": "null"}]},
                    },
                    "required": ["action", "duration_ms", "angle_deg", "speed"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["commands"],
        "additionalProperties": False,
    },
}

SYSTEM_PROMPT = (
    "You are a command parser. Extract an ORDERED list of car-driving commands from the user's utterance.\n"
    "Return ONLY a compact JSON object with a single key 'commands', which is an array of objects.\n"
    "Each object must have ALL of these keys: action, duration_ms, angle_deg, speed.\n"
    "Rules per item:\n"
    "- action ∈ ['forward','backward','left','right','stop'].\n"
    "- If action is 'left' or 'right': angle_deg is an integer 1..360, duration_ms is null.\n"
    "- If action is 'forward' or 'backward': duration_ms is an integer 0..1000, angle_deg is null.\n"
    "- If action is 'stop': both duration_ms and angle_deg are null.\n"
    "- speed is optional: set to 'L0'/'L1'/'L2'/'L3' if specified, otherwise null.\n"
    "Do not include any other keys. Preserve the order the user spoke."
)

# ================== Vosk 配置 ==================
DEFAULT_MODEL_PATH = os.environ.get("VOSK_MODEL_PATH", "./local_processing/vosk_models/en-small")
ENV_DEVICE_INDEX   = os.environ.get("VOSK_DEVICE_INDEX")

# 简单能量 VAD：过滤完全静音帧，减轻 Vosk 压力
VAD_ENERGY_THRESHOLD = -45.0  # dB
VAD_HISTORY_FRAMES   = 10
VAD_LOOKBACK_FRAMES  = 15

model = None
recognizer = None
audio_queue: "queue.Queue[bytes]" = queue.Queue()
segment_queue: "queue.Queue[Tuple[int, str, float]]" = queue.Queue()
stop_event = threading.Event()
segment_counter = 0
counter_lock = threading.Lock()
segment_start_time = None
fallback_segment_start = None
start_lock = threading.Lock()

energy_history = deque(maxlen=VAD_HISTORY_FRAMES)
audio_buffer = deque(maxlen=VAD_LOOKBACK_FRAMES)
is_speaking = False


# ================== 通用工具 ==================
def ensure_token() -> str:
    tok = API_KEY
    if not tok:
        print("❌ Env OPENAI_API_KEY missing.", file=sys.stderr)
        sys.exit(2)
    return tok


def calculate_energy_db(audio_bytes: bytes) -> float:
    audio_array = np.frombuffer(audio_bytes, dtype=np.int16).astype(np.float32) / 32768.0
    rms = np.sqrt(np.mean(np.square(audio_array)) + 1e-8)
    return 20.0 * np.log10(rms + 1e-8)


# ================== JSON 校验（和 mac_realtime_cerebras 一致） ==================
def validate_payload_multi(data: Dict[str, Any]) -> Dict[str, Any]:
    if list(data.keys()) != ["commands"]:
        raise SystemExit(f"❌ Top-level must contain only 'commands'. Got keys: {sorted(list(data.keys()))}")
    cmds = data["commands"]
    if not isinstance(cmds, list) or not (1 <= len(cmds) <= MAX_COMMANDS):
        raise SystemExit(f"❌ 'commands' must be an array with 1..{MAX_COMMANDS} items.")

    for idx, item in enumerate(cmds):
        if not isinstance(item, dict):
            raise SystemExit(f"❌ commands[{idx}] must be an object.")
        expected = {"action", "duration_ms", "angle_deg", "speed"}
        if set(item.keys()) != expected:
            missing = expected - set(item.keys())
            extra = set(item.keys()) - expected
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

        if action in {"left", "right"}:
            if angle_deg is None:
                raise SystemExit(f"❌ commands[{idx}] angle_deg must be integer for turn.")
            if not isinstance(angle_deg, int) or not (1 <= angle_deg <= 360):
                raise SystemExit(f"❌ commands[{idx}] invalid angle_deg: {angle_deg}")
            if duration_ms is not None:
                raise SystemExit(f"❌ commands[{idx}] duration_ms must be null for turn.")
        elif action in {"forward", "backward"}:
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

        item["action"] = action

    return data


class ModelValidationError(Exception):
    """Wrap validation/JSON errors while retaining the raw LLM response."""

    def __init__(self, message: str, raw_content: Optional[str] = None):
        super().__init__(message)
        self.raw_content = raw_content


def call_model_strict_multi(client: OpenAI, user_text: str) -> Tuple[Dict[str, Any], float, str]:
    try:
        t0 = time.perf_counter()
        resp = client.chat.completions.create(
            model=MODEL_NAME,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_text},
            ],
            temperature=0,
            response_format={"type": "json_schema", "json_schema": CAR_CMDS_SCHEMA},
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
        raise ModelValidationError("❌ Non-JSON model response.", raw_content=content)

    try:
        validated = validate_payload_multi(data)
    except SystemExit as e:
        raise ModelValidationError(str(e), raw_content=content)

    return validated, round(model_call_ms, 2), content


# ================== Vosk 音频 & 识别 ==================
def audio_callback(indata, frames, time_info, status):
    """只做轻量级 VAD 过滤，把可能是语音的块送进队列"""
    global is_speaking, energy_history, audio_buffer, segment_start_time, fallback_segment_start

    if stop_event.is_set():
        return

    audio_bytes = bytes(indata)
    energy_db = calculate_energy_db(audio_bytes)
    energy_history.append(energy_db)

    if len(energy_history) == energy_history.maxlen:
        avg_energy = sum(energy_history) / len(energy_history)
        has_voice = avg_energy > VAD_ENERGY_THRESHOLD

        if not is_speaking:
            audio_buffer.append(audio_bytes)
            if has_voice:
                for buf in audio_buffer:
                    audio_queue.put(buf)
                audio_buffer.clear()
                ts = time.time()
                with start_lock:
                    segment_start_time = ts
                    fallback_segment_start = ts
                is_speaking = True
        else:
            audio_queue.put(audio_bytes)
            is_speaking = has_voice  # 静音一段时间就会回到 False


def recognition_worker(debug: bool = False):
    """消费 audio_queue，用 Vosk 自动检测结束，输出整句文本到 segment_queue"""
    global recognizer, segment_counter, segment_start_time, fallback_segment_start

    if debug:
        print("🎙️  Vosk recognition worker started.")
        print(f"    VAD energy threshold: {VAD_ENERGY_THRESHOLD} dB")

    while not stop_event.is_set():
        try:
            data = audio_queue.get(timeout=0.1)
        except queue.Empty:
            continue

        done = recognizer.AcceptWaveform(data)
        if done:
            result = json.loads(recognizer.Result())
            text = result.get("text", "").strip()

            if debug:
                print(f"\n{'='*60}")
                print("🎤 完整语音段识别结果:")
                print(f"  Text: '{text}'")

            local_vosk_ms = None
            with start_lock:
                start_time = segment_start_time if segment_start_time is not None else fallback_segment_start
                segment_start_time = None
                fallback_segment_start = time.time()
            if start_time is not None:
                local_vosk_ms = max(0.0, (time.time() - start_time) * 1000.0)

            if not text:
                if debug:
                    print("  ⚠️ 文本为空，丢弃")
                continue

            with counter_lock:
                segment_counter += 1
                seg_id = segment_counter

            segment_queue.put((seg_id, text, local_vosk_ms))

            if debug:
                print(f"  ✓ 已送往 LLM 解析，segment #{seg_id}")
                print(f"{'='*60}")
        else:
            if debug:
                partial = json.loads(recognizer.PartialResult()).get("partial", "")
                if partial:
                    print(f"\r💭 {partial:<50}", end="", flush=True)


# ================== LLM 线程 ==================
def llm_worker(print_stt: bool = True):
    token = ensure_token()
    t0 = time.perf_counter()
    client = OpenAI(base_url=ENDPOINT, api_key=token)
    client_init_ms = (time.perf_counter() - t0) * 1000.0

    while not stop_event.is_set():
        try:
            seg_id, text, local_vosk_ms = segment_queue.get(timeout=0.1)
        except queue.Empty:
            continue

        if print_stt:
            print(f"👤 STT (segment #{seg_id}): {text}")

        t_model = time.perf_counter()
        raw_response = None
        try:
            data, model_call_ms, raw_response = call_model_strict_multi(client, text)
            model_call_failed = False
        except ModelValidationError as e:
            model_call_ms = (time.perf_counter() - t_model) * 1000.0
            model_call_failed = True
            validation_error = e
            data = None
            raw_response = e.raw_content
        except Exception as e:
            model_call_ms = (time.perf_counter() - t_model) * 1000.0
            model_call_failed = True
            validation_error = e
            data = None

        if raw_response is not None:
            print(f"🤖 LLM raw (segment #{seg_id}): {raw_response}")

        local_vosk_s = (local_vosk_ms or 0.0) / 1000.0
        init_openai_s = client_init_ms / 1000.0
        llm_call_s = model_call_ms / 1000.0
        total_s = local_vosk_s + init_openai_s + llm_call_s

        local_vosk_str = f"{local_vosk_s:.3f}s" if local_vosk_ms is not None else "N/A"
        print(
            f"⏱️  Timing (segment #{seg_id}): "
            f"local_vosk={local_vosk_str}, "
            f"init_openai_client={init_openai_s:.3f}s, "
            f"LLM_call={llm_call_s:.3f}s, Total={total_s:.3f}s",
            file=sys.stderr,
        )

        if not model_call_failed and data is not None:
            for cmd in data["commands"]:
                cmd["id"] = seg_id
                print(json.dumps(cmd, ensure_ascii=False, separators=(",", ":")))
        else:
            if isinstance(validation_error, SystemExit):
                print(
                    f"❌ Command validation failed (segment #{seg_id}): {validation_error}",
                    file=sys.stderr,
                )
            else:
                print(
                    f"❌ Command pipeline error (segment #{seg_id}): {validation_error}",
                    file=sys.stderr,
                )

        segment_queue.task_done()


# ================== 初始化 Vosk ==================
def init_vosk(model_path: str):
    global model, recognizer
    try:
        model = Model(model_path)
    except Exception as e:
        print(f"Failed to load Vosk model from: {model_path}", file=sys.stderr)
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    try:
        recognizer = KaldiRecognizer(model, 16000)
        recognizer.SetWords(True)
        print("Vosk recognizer initialized (auto segment).")
    except Exception as e:
        print(f"Failed to initialize recognizer: {e}", file=sys.stderr)
        sys.exit(1)


# ================== 主程序 ==================
def main():
    parser = argparse.ArgumentParser(
        description="Realtime Vosk → Cerebras LLM → STRICT JSON command array"
    )
    parser.add_argument("--model", default=DEFAULT_MODEL_PATH, help="Path to Vosk model directory")
    parser.add_argument("--device", type=int, default=None, help="sounddevice input device index")
    parser.add_argument("--debug", action="store_true", help="Enable Vosk debug output")
    parser.add_argument("--no-print-stt", action="store_true", help="Do not print recognized text")
    args = parser.parse_args()

    # 选择设备
    if args.device is not None:
        device_index = args.device
    else:
        if ENV_DEVICE_INDEX and ENV_DEVICE_INDEX.isdigit():
            device_index = int(ENV_DEVICE_INDEX)
        else:
            try:
                device_index = sd.default.device[0]
            except Exception:
                device_index = None

    print("🎙️  Realtime Vosk → LLM (Cerebras) command mode.")
    print(f"   - Vosk model: {args.model}")
    print(f"   - Device: {device_index if device_index is not None else '(default)'}")
    print(f"   - Debug: {'ON' if args.debug else 'OFF'}")
    print("Press Ctrl+C to stop.\n")

    init_vosk(args.model)

    # 启动识别线程
    t_rec = threading.Thread(target=recognition_worker, kwargs={"debug": args.debug}, daemon=True)
    t_rec.start()

    # 启动 LLM 线程
    t_llm = threading.Thread(
        target=llm_worker, kwargs={"print_stt": not args.no_print_stt}, daemon=True
    )
    t_llm.start()

    try:
        with sd.RawInputStream(
            samplerate=16000,
            blocksize=8000,
            dtype="int16",
            channels=1,
            callback=audio_callback,
            device=device_index,
        ):
            print("🔴 录音中... (Vosk 自动分段，每段发给 LLM)\n")
            while True:
                sd.sleep(100)
    except KeyboardInterrupt:
        print("\n\n⏹️  停止录音...")
        stop_event.set()
        t_rec.join(timeout=1.0)
        t_llm.join(timeout=1.0)
        print("✓ 已退出")
        sys.exit(0)


if __name__ == "__main__":
    main()
