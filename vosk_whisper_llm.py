#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Realtime dual-path Vosk recognition (grammar + no-grammar) with optional Whisper upgrades
before handing transcripts to a Cerebras/OpenAI-compatible LLM for strict JSON parsing.
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
from faster_whisper import WhisperModel
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
    "You are a command parser. Extract an ORDERED list of car-driving commands from a single automatic speech transcript.\n"
    "Return ONLY a compact JSON object with a single key 'commands', which is an array of objects.\n"
    "Each object must have ALL of these keys: action, duration_ms, angle_deg, speed.\n"
    "Rules per item:\n"
    "- action ∈ ['forward','backward','left','right','stop'].\n"
    "- If action is 'left' or 'right': angle_deg is an integer 1..360, duration_ms is null.\n"
    "- If action is 'forward' or 'backward': duration_ms is an integer 0..2000, angle_deg is null.\n"
    "- If action is 'stop': both duration_ms and angle_deg are null.\n"
    "- speed is optional: set to 'L0'/'L1'/'L2'/'L3' if specified, otherwise null.\n"
    "Do not include any other keys. Preserve the order the user spoke."
)

# ================== Vosk 配置 ==================
DEFAULT_MODEL_PATH = os.environ.get("VOSK_MODEL_PATH", "./vosk_models/en-small")
ENV_DEVICE_INDEX   = os.environ.get("VOSK_DEVICE_INDEX")
GRAMMAR = GRAMMAR_DEFAULT

LLM_MAX_WORKERS = int(os.environ.get("LLM_MAX_WORKERS", "3"))

# ================== Whisper 配置 ==================
WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "base")
WHISPER_DEVICE = os.environ.get("WHISPER_DEVICE", "cpu")
WHISPER_COMPUTE_TYPE = os.environ.get("WHISPER_COMPUTE_TYPE", "int8")
WHISPER_LANGUAGE_HINT = os.environ.get("WHISPER_LANGUAGE", "en")
WHISPER_MAX_WORKERS = int(os.environ.get("WHISPER_MAX_WORKERS", "1"))
WHISPER_PAD_SECONDS = float(os.environ.get("WHISPER_PAD_SECONDS", "0.2"))
AUDIO_RING_SECONDS = int(os.environ.get("AUDIO_RING_SECONDS", "60"))
SAMPLE_RATE = 16000
BYTES_PER_SAMPLE = 2
AUDIO_BLOCK_SIZE = int(os.environ.get("AUDIO_BLOCK_SIZE", "4096"))
AUDIO_BLOCK_SIZE = max(512, min(AUDIO_BLOCK_SIZE, 4000))
AUDIO_QUEUE_MAX = int(os.environ.get("AUDIO_QUEUE_MAX", "18"))
AUDIO_QUEUE_MAX = max(3, AUDIO_QUEUE_MAX)

model = None
rec_grammar = None
rec_no_grammar = None
# Bound audio queue prevents unbounded growth when block size shrinks.
audio_queue: "queue.Queue[bytes]" = queue.Queue(maxsize=AUDIO_QUEUE_MAX)
segment_queue: "queue.Queue[Dict[str, Any]]" = queue.Queue()
whisper_queue: "queue.Queue[Dict[str, Any]]" = queue.Queue()
stop_event = threading.Event()
segment_counter = 0
counter_lock = threading.Lock()
whisper_lock = threading.Lock()
whisper_model = None

pending_segments = deque()
pending_lock = threading.Lock()
pending_event = threading.Event()
llm_active_workers = 0
llm_active_lock = threading.Lock()

LLM_ENQUEUE_THRESHOLD = int(
    os.environ.get("LLM_ENQUEUE_THRESHOLD", str(LLM_MAX_WORKERS))
)
LLM_ENQUEUE_THRESHOLD = max(1, min(LLM_ENQUEUE_THRESHOLD, LLM_MAX_WORKERS))

audio_history = deque()
audio_history_lock = threading.Lock()
audio_frame_cursor = 0
max_audio_frames = AUDIO_RING_SECONDS * SAMPLE_RATE

COMMAND_FIELDS = ("action", "duration_ms", "angle_deg", "speed")


# ================== 通用工具 ==================
def ensure_token() -> str:
    tok = API_KEY
    if not tok:
        print("❌ Env OPENAI_API_KEY missing.", file=sys.stderr)
        sys.exit(2)
    return tok


def store_audio_history(audio_bytes: bytes):
    global audio_frame_cursor
    frames = len(audio_bytes) // BYTES_PER_SAMPLE
    if frames <= 0:
        return
    with audio_history_lock:
        chunk = {
            "start": audio_frame_cursor,
            "end": audio_frame_cursor + frames,
            "data": bytes(audio_bytes),
        }
        audio_history.append(chunk)
        audio_frame_cursor += frames
        min_frame = audio_frame_cursor - max_audio_frames
        while audio_history and audio_history[0]["end"] < min_frame:
            audio_history.popleft()


def extract_audio_segment(start_s: Optional[float], end_s: Optional[float]) -> Optional[bytes]:
    if start_s is None or end_s is None:
        return None
    start_s = max(0.0, start_s - WHISPER_PAD_SECONDS)
    end_s = max(start_s + 1e-3, end_s + WHISPER_PAD_SECONDS)
    start_frame = int(start_s * SAMPLE_RATE)
    end_frame = int(end_s * SAMPLE_RATE)
    if end_frame <= start_frame:
        return None
    return extract_audio_segment_frames(start_frame, end_frame)


def extract_audio_segment_frames(start_frame: Optional[int], end_frame: Optional[int]) -> Optional[bytes]:
    if start_frame is None or end_frame is None:
        return None
    start_frame = max(0, start_frame)
    end_frame = max(start_frame + 1, end_frame)
    with audio_history_lock:
        if not audio_history:
            return None
        pieces = []
        for chunk in audio_history:
            chunk_start = chunk["start"]
            chunk_end = chunk["end"]
            if chunk_end <= start_frame:
                continue
            if chunk_start >= end_frame:
                break
            rel_start = max(0, start_frame - chunk_start)
            rel_end = min(chunk_end - chunk_start, end_frame - chunk_start)
            if rel_end <= rel_start:
                continue
            mv = memoryview(chunk["data"])
            pieces.append(bytes(mv[rel_start * BYTES_PER_SAMPLE : rel_end * BYTES_PER_SAMPLE]))
        if not pieces:
            return None
        return b"".join(pieces)


def emit_command_payload(cmd: Dict[str, Any], seg_id: int):
    """Print a single command JSON line compatible with vosk_cerebras output."""
    payload = {field: cmd.get(field) for field in COMMAND_FIELDS}
    payload["id"] = seg_id
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))


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
                duration_ms = 0.5
                #raise SystemExit(f"❌ commands[{idx}] duration_ms must be integer for straight move.")
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


def call_model_strict(client: OpenAI, transcript_text: str) -> Tuple[Dict[str, Any], float, str]:
    try:
        t0 = time.perf_counter()
        user_prompt = (
            "Transcript:\n"
            f"{transcript_text if transcript_text else '[empty]'}\n\n"
            "Produce car-driving commands that obey the schema."
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
    """Push raw PCM into the Vosk queue and maintain history for Whisper."""
    if stop_event.is_set():
        return

    audio_bytes = bytes(indata)
    store_audio_history(audio_bytes)
    try:
        audio_queue.put_nowait(audio_bytes)
    except queue.Full:
        try:
            audio_queue.get_nowait()
        except queue.Empty:
            pass
        try:
            audio_queue.put_nowait(audio_bytes)
        except queue.Full:
            pass


def recognition_worker(debug: bool = False):
    """Dual-path Vosk recognition; perfect matches are parsed locally, others go Whisper/LLM."""
    global rec_grammar, rec_no_grammar, segment_counter

    if debug:
        print("🎙️  Vosk recognition worker started (grammar + no-grammar).")

    def tokenize(text: str):
        return [tok for tok in text.strip().split() if tok]

    def is_single_stop(tokens) -> bool:
        return len(tokens) == 1 and tokens[0].lower() == "stop"

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

    while not stop_event.is_set():
        try:
            data = audio_queue.get(timeout=0.1)
        except queue.Empty:
            continue

        grammar_done = rec_grammar.AcceptWaveform(data)
        no_grammar_done = rec_no_grammar.AcceptWaveform(data)

        if grammar_done and no_grammar_done:
            grammar_result = json.loads(rec_grammar.Result())
            no_grammar_result = json.loads(rec_no_grammar.Result())

            grammar_text = grammar_result.get("text", "").strip()
            no_grammar_text = no_grammar_result.get("text", "").strip()

            grammar_tokens = tokenize(grammar_text)
            no_grammar_tokens = tokenize(no_grammar_text)
            max_words = max(len(grammar_tokens), len(no_grammar_tokens))

            if max_words <= 1 and not (
                grammar_tokens and is_single_stop(grammar_tokens)
            ) and not (no_grammar_tokens and is_single_stop(no_grammar_tokens)):
                if debug:
                    print("  ⚠️ 词数过少（≤1 且非 'stop'），跳过该段")
                continue

            (
                grammar_word_details,
                grammar_avg_conf,
                grammar_start_s,
                grammar_end_s,
            ) = extract_confidence(grammar_result)
            (
                no_grammar_word_details,
                no_grammar_avg_conf,
                no_grammar_start_s,
                no_grammar_end_s,
            ) = extract_confidence(no_grammar_result)
            segment_start_s = (
                no_grammar_start_s if no_grammar_start_s is not None else grammar_start_s
            )
            segment_end_s = no_grammar_end_s if no_grammar_end_s is not None else grammar_end_s

            if debug:
                print(f"\n{'='*60}")
                print("🎤 检测到语音段")
                print(f"  Grammar: '{grammar_text}'")
                print(f"  No-Grammar: '{no_grammar_text}'")
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

            with counter_lock:
                segment_counter += 1
                seg_id = segment_counter

            fallback_reasons = []
            grammar_perfect = bool(grammar_word_details) and all(
                conf == 1.0 for _, conf in grammar_word_details
            )
            no_grammar_perfect = bool(no_grammar_word_details) and all(
                conf == 1.0 for _, conf in no_grammar_word_details
            )
            transcripts_match = (
                grammar_text
                and no_grammar_text
                and grammar_text.lower() == no_grammar_text.lower()
            )

            local_output_emitted = False
            if grammar_perfect and grammar_text:
                try:
                    local_command = to_structured(grammar_text, debug=debug)
                    emit_command_payload(local_command, seg_id)
                    local_output_emitted = True
                    if debug:
                        print(f"  ✅ Local parser emitted command for segment #{seg_id}")
                except Exception as exc:
                    fallback_reasons.append(f"parse-error:{exc}")
                    if debug:
                        print(f"  ❌ 本地解析失败：{exc}")
            else:
                if not transcripts_match:
                    fallback_reasons.append("grammar-mismatch")
                if not grammar_perfect:
                    fallback_reasons.append("vosk-grammar-low-confidence")
                if not no_grammar_perfect:
                    fallback_reasons.append("vosk-no-grammar-low-confidence")
                if not grammar_text or not no_grammar_text:
                    fallback_reasons.append("empty-transcript")

            use_whisper = False
            send_direct_llm = False
            if not local_output_emitted:
                missing_timestamps = segment_start_s is None or segment_end_s is None
                if missing_timestamps:
                    fallback_reasons.append("missing-timestamps")
                    send_direct_llm = True
                elif no_grammar_perfect and no_grammar_text:
                    fallback_reasons.append("no-grammar-perfect")
                    send_direct_llm = True
                else:
                    use_whisper = True

            if debug:
                start_str = f"{segment_start_s:.3f}s" if segment_start_s is not None else "N/A"
                end_str = f"{segment_end_s:.3f}s" if segment_end_s is not None else "N/A"
                print(f"  ⏱️ Segment #{seg_id} timing: {start_str} → {end_str}")

            if use_whisper:
                segment_info = {
                    "id": seg_id,
                    "transcript_text": no_grammar_text,
                    "local_vosk_ms": None,
                    "fallback_reasons": list(fallback_reasons),
                    "segment_start_s": segment_start_s,
                    "segment_end_s": segment_end_s,
                }
                if debug:
                    print("  🌀 发送至 Whisper 队列，等待高精度转写")
                whisper_queue.put(segment_info)
            elif send_direct_llm and not local_output_emitted:
                segment_info = {
                    "id": seg_id,
                    "transcript_text": no_grammar_text,
                    "local_vosk_ms": None,
                    "fallback_reasons": list(fallback_reasons),
                    "segment_start_s": segment_start_s,
                    "segment_end_s": segment_end_s,
                }
                enqueue_segment_for_llm(segment_info)
            elif not local_output_emitted:
                segment_info = {
                    "id": seg_id,
                    "transcript_text": no_grammar_text,
                    "local_vosk_ms": None,
                    "fallback_reasons": list(fallback_reasons),
                    "segment_start_s": segment_start_s,
                    "segment_end_s": segment_end_s,
                }
                enqueue_segment_for_llm(segment_info)
            else:
                if debug:
                    print(f"  ✅ 本地 JSON 已输出，segment #{seg_id}")

            if fallback_reasons and debug and not local_output_emitted:
                print(f"  ⚠️ 触发 Whisper/LLM 回退，segment #{seg_id}")
                print(f"{'='*60}")
        else:
            if debug:
                partial = json.loads(rec_no_grammar.PartialResult()).get("partial", "")
                if partial:
                    print(f"\r💭 {partial:<50}", end="", flush=True)


def whisper_worker(worker_id: int, debug: bool = False):
    """Consume fallback segments, cut audio, run Whisper, then forward to LLM queue."""
    # Keep STT logging centralized inside llm_worker to avoid duplicate lines.
    global whisper_model
    while not stop_event.is_set():
        try:
            segment_info = whisper_queue.get(timeout=0.1)
        except queue.Empty:
            continue

        seg_id = segment_info.get("id")
        start_s = segment_info.get("segment_start_s")
        end_s = segment_info.get("segment_end_s")
        whisper_text = None
        pcm_bytes = extract_audio_segment(start_s, end_s)
        if whisper_model is None:
            if debug:
                print("  ❌ Whisper model not initialized，丢弃该段")
            whisper_queue.task_done()
            continue
        if pcm_bytes is None:
            if debug:
                print(f"  ⚠️ 无法截取音频，丢弃 segment #{seg_id}")
            whisper_queue.task_done()
            continue

        pcm_np = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32) / 32768.0
        try:
            with whisper_lock:
                t_whisper = time.perf_counter()
                segments, _ = whisper_model.transcribe(
                    pcm_np,
                    language=WHISPER_LANGUAGE_HINT,
                    without_timestamps=True,
                    vad_filter=False,
                    beam_size=1,
                )
            whisper_ms = (time.perf_counter() - t_whisper) * 1000.0
            whisper_text = "".join(seg.text for seg in segments).strip()
        except Exception as exc:
            if debug:
                print(f"  ❌ Whisper failed on segment #{seg_id}: {exc}")
            whisper_ms = None

        if not whisper_text:
            if debug:
                print(f"  ⚠️ Whisper 空结果，丢弃 segment #{seg_id}")
            whisper_queue.task_done()
            continue

        segment_info["transcript_text"] = whisper_text
        segment_info["used_whisper"] = True
        if whisper_ms is not None:
            segment_info["whisper_transcribe_ms"] = round(whisper_ms, 2)
        if debug:
            print(f"  🌀 Whisper text (segment #{seg_id}): {whisper_text}")

        enqueue_segment_for_llm(segment_info)
        whisper_queue.task_done()

    # drain remaining items when stopping
    while True:
        try:
            segment_info = whisper_queue.get_nowait()
        except queue.Empty:
            break
        enqueue_segment_for_llm(segment_info)
        whisper_queue.task_done()


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
            transcript_text = segment_info.get("transcript_text", "")
            local_vosk_ms = segment_info.get("local_vosk_ms")
            fallback_reasons = segment_info.get("fallback_reasons", [])
            whisper_transcribe_ms = segment_info.get("whisper_transcribe_ms")
            used_whisper = segment_info.get("used_whisper", False)

            if print_stt:
                source = "Whisper" if used_whisper else "Vosk"
                print(f"🗣️ {source} STT (segment #{seg_id}): {transcript_text}")
                if fallback_reasons:
                    print(
                        f"⚠️  Fallback reasons (segment #{seg_id}): "
                        + ", ".join(fallback_reasons)
                    )

            t_model = time.perf_counter()
            raw_response = None
            validation_error: Optional[Exception] = None
            try:
                data, model_call_ms, raw_response = call_model_strict(
                    shared_client, transcript_text
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
            whisper_transcribe_s = (whisper_transcribe_ms or 0.0) / 1000.0
            init_openai_s = shared_client_init_ms / 1000.0
            llm_call_s = model_call_ms / 1000.0
            total_s = local_vosk_s + whisper_transcribe_s + init_openai_s + llm_call_s

            local_vosk_str = f"{local_vosk_s:.3f}s" if local_vosk_ms is not None else "N/A"
            whisper_str = (
                f"{whisper_transcribe_s:.3f}s" if whisper_transcribe_ms is not None else "N/A"
            )
            print(
                f"⏱️  Timing (segment #{seg_id}): "
                f"local_vosk={local_vosk_str}, "
                f"whisper_transcribe={whisper_str}, "
                f"init_openai_client={init_openai_s:.3f}s, "
                f"LLM_call={llm_call_s:.3f}s, Total={total_s:.3f}s",
                file=sys.stderr,
            )

            if not model_call_failed and data is not None:
                for cmd in data["commands"]:
                    emit_command_payload(cmd, seg_id)
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
        rec_grammar = KaldiRecognizer(model, SAMPLE_RATE, json.dumps(GRAMMAR))
        rec_grammar.SetWords(True)

        rec_no_grammar = KaldiRecognizer(model, SAMPLE_RATE)
        rec_no_grammar.SetWords(True)

        print("Dual-path Vosk recognizers initialized (grammar + no-grammar).")
    except Exception as e:
        print(f"Failed to initialize recognizer: {e}", file=sys.stderr)
        sys.exit(1)


def init_whisper():
    global whisper_model
    try:
        print(
            f"Whisper model loading... ({WHISPER_MODEL}, device={WHISPER_DEVICE}, compute={WHISPER_COMPUTE_TYPE})"
        )
        whisper_model = WhisperModel(
            WHISPER_MODEL,
            device=WHISPER_DEVICE,
            compute_type=WHISPER_COMPUTE_TYPE,
        )
        print("Whisper model ready.")
    except Exception as e:
        print(f"Failed to load Whisper model: {e}", file=sys.stderr)
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

    print("🎙️  Realtime dual-path Vosk → local JSON or Whisper+LLM fallback.")
    print(f"   - Vosk model: {args.model}")
    print(f"   - Device: {device_index if device_index is not None else '(default)'}")
    print(f"   - Debug: {'ON' if args.debug else 'OFF'}")
    block_ms = (AUDIO_BLOCK_SIZE / SAMPLE_RATE) * 1000.0
    print(f"   - Audio blocksize: {AUDIO_BLOCK_SIZE} frames (~{block_ms:.0f} ms)")
    print("   - Local: grammar/no-grammar perfect matches parsed via `parse_en`")
    print("   - Fallback: optional Whisper upgrade before Cerebras/OpenAI LLM")
    print("Press Ctrl+C to stop.\n")

    init_vosk(args.model)
    init_whisper()

    # LLM 节流线程，负责把待处理段落送入 LLM 队列
    t_throttle = threading.Thread(target=throttle_worker, kwargs={"debug": args.debug}, daemon=True)
    t_throttle.start()

    # Whisper 线程（可配置并发）
    whisper_threads = []
    for idx in range(max(1, WHISPER_MAX_WORKERS)):
        t_whisper = threading.Thread(
            target=whisper_worker,
            kwargs={
                "worker_id": idx,
                "debug": args.debug,
            },
            daemon=True,
        )
        t_whisper.start()
        whisper_threads.append(t_whisper)

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
            samplerate=SAMPLE_RATE,
            blocksize=AUDIO_BLOCK_SIZE,
            dtype="int16",
            channels=1,
            callback=audio_callback,
            device=device_index,
        ):
            print("🔴 录音中... (dual-path Vosk + optional Whisper + LLM)\n")
            while True:
                sd.sleep(100)
    except KeyboardInterrupt:
        print("\n\n⏹️  停止录音...")
        stop_event.set()
        t_rec.join(timeout=1.0)
        t_llm.join(timeout=1.0)
        t_throttle.join(timeout=1.0)
        for t_whisper in whisper_threads:
            t_whisper.join(timeout=1.0)
        print("✓ 已退出")
        sys.exit(0)


if __name__ == "__main__":
    main()
