#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Realtime dual-path Vosk recognition with both local and Cerebras/OpenAI parsing.

Pipeline:
    - 双识别器（Grammar + No-Grammar）并行校验语音段
    - Cross-check 通过：立即输出本地 `parse_en.to_structured` JSON（含 segment id）并跳过 LLM
    - Cross-check 未通过：仅将语音段送往 LLM（Cerebras / OpenAI-compatible）异步解析
"""

import os
import sys
import json
import time
import argparse
import threading
import queue
from collections import deque
from typing import Dict, Any, Optional, Tuple

import numpy as np
import sounddevice as sd
from vosk import Model, KaldiRecognizer
from openai import OpenAI
from dotenv import load_dotenv
from parse_en import to_structured
from vosk_config import GRAMMAR_DEFAULT

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
    "You are a command parser. Extract an ORDERED list of car-driving commands from automatic speech transcripts.\n"
    "You will receive two transcripts per segment: one produced with a constrained grammar and one without grammar.\n"
    "Choose the transcript that best matches a plausible command sequence; they usually agree but may differ slightly.\n"
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
GRAMMAR = GRAMMAR_DEFAULT

LLM_MAX_WORKERS = int(os.environ.get("LLM_MAX_WORKERS", "3"))

# 简单能量 VAD：过滤完全静音帧，减轻 Vosk 压力
VAD_ENERGY_THRESHOLD = -40.0  # dB
VAD_HISTORY_FRAMES   = 10
VAD_LOOKBACK_FRAMES  = 15

model = None
rec_grammar = None
rec_no_grammar = None
audio_queue: "queue.Queue[bytes]" = queue.Queue()
segment_queue: "queue.Queue[Dict[str, Any]]" = queue.Queue()
result_queue: "queue.Queue[Dict[str, Any]]" = queue.Queue()
stop_event = threading.Event()
segment_counter = 0
counter_lock = threading.Lock()
segment_start_time = None
fallback_segment_start = None
start_lock = threading.Lock()

pending_segments = deque()
pending_lock = threading.Lock()
pending_event = threading.Event()
llm_active_workers = 0
llm_active_lock = threading.Lock()

LLM_ENQUEUE_THRESHOLD = int(
    os.environ.get("LLM_ENQUEUE_THRESHOLD", str(LLM_MAX_WORKERS))
)
LLM_ENQUEUE_THRESHOLD = max(1, min(LLM_ENQUEUE_THRESHOLD, LLM_MAX_WORKERS))

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


def enqueue_segment_for_llm(segment_info: Dict[str, Any]):
    with pending_lock:
        pending_segments.append(segment_info)
    pending_event.set()


def mark_llm_start():
    global llm_active_workers
    with llm_active_lock:
        llm_active_workers += 1


def mark_llm_end():
    global llm_active_workers
    with llm_active_lock:
        if llm_active_workers > 0:
            llm_active_workers -= 1
    pending_event.set()


def get_llm_active_workers() -> int:
    with llm_active_lock:
        return llm_active_workers


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


def call_model_strict_multi(client: OpenAI, grammar_text: str, no_grammar_text: str) -> Tuple[Dict[str, Any], float, str]:
    try:
        t0 = time.perf_counter()
        user_prompt = (
            "Grammar recognizer transcript:\n"
            f"{grammar_text if grammar_text else '[empty]'}\n\n"
            "No-grammar recognizer transcript:\n"
            f"{no_grammar_text if no_grammar_text else '[empty]'}\n\n"
            "Use whichever transcript is most reliable to produce car-driving commands that obey the schema."
        )
        resp = client.chat.completions.create(
            model=MODEL_NAME,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
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
    """Dual-path Vosk recognition with cross-validation and local parsing."""
    global rec_grammar, rec_no_grammar, segment_counter, segment_start_time, fallback_segment_start

    if debug:
        print("🎙️  Dual-path recognition worker started.")
        print(f"    VAD energy threshold: {VAD_ENERGY_THRESHOLD} dB")

    while not stop_event.is_set():
        try:
            data = audio_queue.get(timeout=0.1)
        except queue.Empty:
            continue

        grammar_done = rec_grammar.AcceptWaveform(data)
        no_grammar_done = rec_no_grammar.AcceptWaveform(data)

        if grammar_done and no_grammar_done:
            with start_lock:
                start_time = segment_start_time if segment_start_time is not None else fallback_segment_start
                segment_start_time = None
                fallback_segment_start = time.time()
            local_vosk_ms = None
            if start_time is not None:
                local_vosk_ms = max(0.0, (time.time() - start_time) * 1000.0)

            grammar_result = json.loads(rec_grammar.Result())
            no_grammar_result = json.loads(rec_no_grammar.Result())

            grammar_text = grammar_result.get("text", "").strip()
            no_grammar_text = no_grammar_result.get("text", "").strip()

            def tokenize(text: str):
                return [tok for tok in text.strip().split() if tok]

            grammar_tokens = tokenize(grammar_text)
            no_grammar_tokens = tokenize(no_grammar_text)
            max_words = max(len(grammar_tokens), len(no_grammar_tokens))

            def is_single_stop(tokens) -> bool:
                return len(tokens) == 1 and tokens[0].lower() == "stop"

            if max_words <= 1 and not (
                is_single_stop(grammar_tokens) or is_single_stop(no_grammar_tokens)
            ):
                if debug:
                    print(
                        "  ⚠️ 词数过少（≤1 且非 'stop'），跳过该段"
                    )
                continue

            if debug:
                print(f"\n{'='*60}")
                print("🎤 检测到语音段")
                print(f"  Grammar: '{grammar_text}'")
                print(f"  No-Grammar: '{no_grammar_text}'")

            def extract_confidence(result: Dict[str, Any]):
                words = []
                start_s = None
                end_s = None
                if "result" in result and result["result"]:
                    for word_info in result["result"]:
                        word = word_info.get("word", "")
                        conf = word_info.get("conf", 0.0)
                        words.append((word, conf))
                        if start_s is None:
                            start_s = word_info.get("start")
                        end_s = word_info.get("end")
                if words:
                    avg_conf = sum(conf for _, conf in words) / len(words)
                    return words, avg_conf, start_s, end_s
                return [], None, None, None

            (
                grammar_word_details,
                grammar_avg_conf,
                _,
                _,
            ) = extract_confidence(grammar_result)
            (
                no_grammar_word_details,
                no_grammar_avg_conf,
                segment_start_s,
                segment_end_s,
            ) = extract_confidence(no_grammar_result)

            if debug:
                if grammar_word_details:
                    print("  Grammar confidences:")
                    for word, conf in grammar_word_details:
                        print(f"    '{word}': {conf:.3f}")
                    print(f"    avg={grammar_avg_conf:.3f}")
                if no_grammar_word_details:
                    print("  No-Grammar confidences:")
                    for word, conf in no_grammar_word_details:
                        print(f"    '{word}': {conf:.3f}")
                    print(f"    avg={no_grammar_avg_conf:.3f}")

            fallback_reasons = []

            all_perfect_no_grammar = False
            if no_grammar_word_details:
                all_perfect_no_grammar = all(conf == 1.0 for _, conf in no_grammar_word_details)

            if all_perfect_no_grammar:
                grammar_words = set(grammar_text.lower().split())
                no_grammar_words = set(no_grammar_text.lower().split())
                is_subset = grammar_words.issubset(no_grammar_words)
                if debug:
                    print(f"  Grammar ⊆ No-Grammar: {is_subset}")
                if not is_subset:
                    if debug:
                        missing = grammar_words - no_grammar_words
                        if missing:
                            print(f"  ⚠️ 交叉验证失败，改用 LLM: {sorted(missing)}")
                    fallback_reasons.append("grammar-not-subset")

            grammar_all_perfect = bool(grammar_word_details) and all(
                conf == 1.0 for _, conf in grammar_word_details
            )
            if not grammar_all_perfect:
                if debug:
                    print("  ⚠️ Grammar 置信度不完美，改用 LLM")
                fallback_reasons.append("grammar-confidence")

            if not grammar_text:
                if debug:
                    print("  ⚠️ Grammar 文本为空，改用 LLM")
                fallback_reasons.append("empty-grammar")

            with counter_lock:
                segment_counter += 1
                seg_id = segment_counter

            if debug:
                start_str = f"{segment_start_s:.3f}s" if segment_start_s is not None else "N/A"
                end_str = f"{segment_end_s:.3f}s" if segment_end_s is not None else "N/A"
                print(f"  ⏱️ Segment #{seg_id} timing (no-grammar): {start_str} → {end_str}")

            local_output_emitted = False
            if not fallback_reasons:
                try:
                    local_command = to_structured(grammar_text, debug=debug)
                    local_command["id"] = seg_id
                    result_queue.put(local_command)
                    local_output_emitted = True
                    if debug:
                        print("  ✓ 本地解析通过，已输出 Local raw")
                except Exception as exc:
                    if debug:
                        print(f"  ❌ 本地解析失败，改用 LLM: {exc}")
                    fallback_reasons.append("parse-error")

            needs_llm = bool(fallback_reasons) or not local_output_emitted

            if needs_llm:
                enqueue_segment_for_llm(
                    {
                        "id": seg_id,
                        "grammar_text": grammar_text,
                        "no_grammar_text": no_grammar_text,
                        "local_vosk_ms": local_vosk_ms,
                        "fallback_reasons": fallback_reasons,
                        "segment_start_s": segment_start_s,
                        "segment_end_s": segment_end_s,
                    }
                )
            elif debug:
                print(f"  ✓ 本地解析可信，跳过 LLM，segment #{seg_id}")

            if fallback_reasons:
                if debug:
                    print(f"  ⚠️ 触发 LLM 回退，segment #{seg_id}")
                    print(f"{'='*60}")
            elif not local_output_emitted and debug:
                print(f"  ⚠️ 本地解析未输出，但仍等待 LLM，segment #{seg_id}")
        else:
            if debug:
                partial = json.loads(rec_no_grammar.PartialResult()).get("partial", "")
                if partial:
                    print(f"\r💭 {partial:<50}", end="", flush=True)


def output_worker(debug: bool = False):
    """持续输出本地解析结果，保持与 vosk_realtime 风格一致。"""
    while not stop_event.is_set():
        try:
            command = result_queue.get(timeout=0.1)
        except queue.Empty:
            continue

        if debug:
            print("\r" + " " * 60 + "\r", end="", flush=True)

        seg_id = command.get("id", "?")
        compact = json.dumps(command, ensure_ascii=False, separators=(",", ":"))
        print(f"📄 Local raw (segment #{seg_id}): {compact}")

        result_queue.task_done()


def throttle_worker(debug: bool = False):
    """控制何时将回退段落送入 LLM 队列，避免并发被占满时持续堆积。"""
    while not stop_event.is_set():
        pending_event.wait(timeout=0.1)
        if stop_event.is_set():
            break

        if get_llm_active_workers() >= LLM_ENQUEUE_THRESHOLD:
            if debug:
                print("🛑 LLM 并发满，等待再入队")
            time.sleep(0.05)
            continue

        segment_info = None
        with pending_lock:
            if pending_segments:
                segment_info = pending_segments.popleft()
                if not pending_segments:
                    pending_event.clear()
            else:
                pending_event.clear()

        if segment_info is None:
            time.sleep(0.01)
            continue

        if debug:
            seg_id = segment_info.get("id", "?")
            print(f"🚦 发送 segment #{seg_id} 至 LLM 队列")
        segment_queue.put(segment_info)
        time.sleep(0.01)

    while True:
        with pending_lock:
            if not pending_segments:
                break
            segment_info = pending_segments.popleft()
        segment_queue.put(segment_info)


# ================== LLM 线程 ==================
def llm_worker(print_stt: bool = True):
    token = ensure_token()
    t_client = time.perf_counter()
    try:
        shared_client = OpenAI(base_url=ENDPOINT, api_key=token)
    except Exception as exc:
        raise SystemExit(f"❌ Failed to initialize OpenAI client: {exc}")
    shared_client_init_ms = (time.perf_counter() - t_client) * 1000.0
    semaphore = threading.Semaphore(LLM_MAX_WORKERS)

    def process_segment(segment_info: Dict[str, Any]):
        mark_llm_start()
        try:
            seg_id = segment_info["id"]
            grammar_text = segment_info.get("grammar_text", "")
            no_grammar_text = segment_info.get("no_grammar_text", "")
            local_vosk_ms = segment_info.get("local_vosk_ms")
            fallback_reasons = segment_info.get("fallback_reasons", [])
            segment_start_s = segment_info.get("segment_start_s")
            segment_end_s = segment_info.get("segment_end_s")

            if print_stt:
                print(f"👤 Grammar STT (segment #{seg_id}): {grammar_text}")
                print(f"👤 No-Grammar STT (segment #{seg_id}): {no_grammar_text}")
                if fallback_reasons:
                    print(
                        f"⚠️  Fallback reasons (segment #{seg_id}): "
                        + ", ".join(fallback_reasons)
                    )

            t_model = time.perf_counter()
            raw_response = None
            validation_error: Optional[Exception] = None
            try:
                data, model_call_ms, raw_response = call_model_strict_multi(
                    shared_client, grammar_text, no_grammar_text
                )
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
            init_openai_s = shared_client_init_ms / 1000.0
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
        finally:
            segment_queue.task_done()
            semaphore.release()
            mark_llm_end()

    active_threads = []

    while not stop_event.is_set():
        try:
            segment_info = segment_queue.get(timeout=0.1)
        except queue.Empty:
            continue

        semaphore.acquire()
        worker = threading.Thread(target=process_segment, args=(segment_info,), daemon=True)
        active_threads.append(worker)
        worker.start()

    # Drain remaining items when stopping
    while True:
        try:
            segment_info = segment_queue.get_nowait()
        except queue.Empty:
            break
        semaphore.acquire()
        worker = threading.Thread(target=process_segment, args=(segment_info,), daemon=True)
        active_threads.append(worker)
        worker.start()

    for worker in active_threads:
        worker.join(timeout=5.0)


# ================== 初始化 Vosk ==================
def init_vosk(model_path: str):
    global model, rec_grammar, rec_no_grammar
    try:
        model = Model(model_path)
    except Exception as e:
        print(f"Failed to load Vosk model from: {model_path}", file=sys.stderr)
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    try:
        rec_grammar = KaldiRecognizer(model, 16000, json.dumps(GRAMMAR))
        rec_grammar.SetWords(True)

        rec_no_grammar = KaldiRecognizer(model, 16000)
        rec_no_grammar.SetWords(True)

        print("Dual-path Vosk recognizers initialized (grammar + no-grammar).")
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

    print("🎙️  Realtime Vosk → Local JSON + Cerebras LLM.")
    print(f"   - Vosk model: {args.model}")
    print(f"   - Device: {device_index if device_index is not None else '(default)'}")
    print(f"   - Debug: {'ON' if args.debug else 'OFF'}")
    print("   - Local: grammar-validated JSON printed immediately when checks pass")
    print("   - Remote: LLM runs in parallel; fallback output used when checks fail")
    print("Press Ctrl+C to stop.\n")

    init_vosk(args.model)

    # LLM 节流线程，负责把待处理段落送入 LLM 队列
    t_throttle = threading.Thread(target=throttle_worker, kwargs={"debug": args.debug}, daemon=True)
    t_throttle.start()

    # 启动识别线程
    t_rec = threading.Thread(target=recognition_worker, kwargs={"debug": args.debug}, daemon=True)
    t_rec.start()

    # 本地输出线程
    t_out = threading.Thread(target=output_worker, kwargs={"debug": args.debug}, daemon=True)
    t_out.start()

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
            print("🔴 录音中... (双路径 Vosk，本地 + LLM 并行解析)\n")
            while True:
                sd.sleep(100)
    except KeyboardInterrupt:
        print("\n\n⏹️  停止录音...")
        stop_event.set()
        t_rec.join(timeout=1.0)
        t_out.join(timeout=1.0)
        t_llm.join(timeout=1.0)
        t_throttle.join(timeout=1.0)
        print("✓ 已退出")
        sys.exit(0)


if __name__ == "__main__":
    main()
