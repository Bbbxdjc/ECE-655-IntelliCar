#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Vosk 录音文件识别测试（参数与 vosk_realtime 对齐）
用法示例：
  python3 vosk_recording.py --audio path/to/test.wav --model ./vosk_models/en-small --debug
环境变量：
  VOSK_MODEL_PATH, VOSK_USE_GRAMMAR（与 vosk_realtime 一致）
"""

import os
import sys
import json
import wave
import subprocess
import shutil
import argparse
import time
import numpy as np
from vosk import Model, KaldiRecognizer
from vosk_config import GRAMMAR_DEFAULT

# 与 vosk_realtime 对齐的默认值
DEFAULT_MODEL_PATH = os.environ.get("VOSK_MODEL_PATH", "./vosk_models/en-small")
DEFAULT_USE_GRAMMAR = os.environ.get("VOSK_USE_GRAMMAR", "1").lower() not in {"0", "false", "no"}


def _downmix_to_mono_int16(raw_bytes: bytes, channels: int) -> bytes:
    """将多通道 int16 PCM 下混为单声道 int16 PCM。"""
    if channels == 1:
        return raw_bytes
    # 以 int16 读取并 reshape 为 (frames, channels)
    arr = np.frombuffer(raw_bytes, dtype=np.int16)
    if len(arr) % channels != 0:
        # 不完整帧，截断到整帧
        frames = len(arr) // channels
        arr = arr[: frames * channels]
    arr = arr.reshape(-1, channels).astype(np.int32)
    mono = (arr.mean(axis=1)).astype(np.int16)
    return mono.tobytes()


def _init_recognizer(model_path: str, sample_rate: int, use_grammar: bool) -> tuple[KaldiRecognizer, float]:
    try:
        t0 = time.perf_counter()
        model = Model(model_path)
        t1 = time.perf_counter()
        load_time = t1 - t0
    except Exception as e:
        print(f"Failed to load Vosk model from: {model_path}", file=sys.stderr)
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    try:
        if use_grammar:
            rec = KaldiRecognizer(model, sample_rate, json.dumps(GRAMMAR_DEFAULT))
        else:
            rec = KaldiRecognizer(model, sample_rate)
        rec.SetWords(True)
        return rec, load_time
    except Exception as e:
        print(f"Failed to initialize recognizer: {e}", file=sys.stderr)
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(description="Vosk Recording File Recognition Test")
    parser.add_argument("--audio", required=True, help="Path to input WAV file (16-bit PCM)")
    parser.add_argument("--model", default=DEFAULT_MODEL_PATH, help="Path to Vosk model directory")
    parser.add_argument(
        "--use-grammar",
        action="store_true" if DEFAULT_USE_GRAMMAR else "store_false",
        help=("Enable grammar decoding (default: {} via VOSK_USE_GRAMMAR)".format(
            "ON" if DEFAULT_USE_GRAMMAR else "OFF"
        )),
    )
    parser.add_argument("--debug", action="store_true", help="Print partial results and details")
    args = parser.parse_args()

    audio_path = args.audio
    model_path = args.model
    use_grammar = args.use_grammar if isinstance(args.use_grammar, bool) else DEFAULT_USE_GRAMMAR

    if not os.path.isfile(audio_path):
        print(f"Audio file not found: {audio_path}", file=sys.stderr)
        sys.exit(1)

    print("🎧  Vosk Recording File Recognition")
    print(f"  Model: {model_path}")
    print(f"  Audio: {audio_path}")
    print(f"  Grammar: {'ON' if use_grammar else 'OFF'}")
    print(f"  Debug: {'ON' if args.debug else 'OFF'}\n")

    # 如果是 WAV 且可直接读取，则走 WAV 流程；否则尝试使用 ffmpeg 解码（支持 m4a/mp3/flac 等）
    def recognize_from_ffmpeg(path: str):
        if not shutil.which("ffmpeg"):
            print("ffmpeg 未安装，无法直接解码 m4a/mp3 等。请先安装 ffmpeg，如: brew install ffmpeg", file=sys.stderr)
            sys.exit(1)

        target_sr = 16000
        print(f"使用 ffmpeg 解码到 mono/{target_sr}Hz 16-bit PCM...")
        cmd = [
            "ffmpeg", "-nostdin", "-loglevel", "quiet", "-i", path,
            "-f", "s16le", "-acodec", "pcm_s16le", "-ac", "1", "-ar", str(target_sr), "-"
        ]
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except Exception as e:
            print(f"启动 ffmpeg 失败: {e}", file=sys.stderr)
            sys.exit(1)

        rec, model_load_seconds = _init_recognizer(model_path, target_sr, use_grammar)

        print("开始识别...\n")
        CHUNK_BYTES = 8000
        bytes_per_second = 2 * 1 * target_sr  # s16le mono
        audio_seconds = 0.0
        model_infer_seconds = 0.0
        while True:
            data = proc.stdout.read(CHUNK_BYTES)
            if not data:
                break
            t0 = time.perf_counter()
            accepted = rec.AcceptWaveform(data)
            model_infer_seconds += time.perf_counter() - t0
            audio_seconds += len(data) / bytes_per_second
            if accepted:
                result = json.loads(rec.Result())
                print("识别结果:", result.get("text", ""))
            else:
                if args.debug:
                    partial = json.loads(rec.PartialResult()).get("partial", "")
                    if partial:
                        print("部分结果:", partial)

        # 读取剩余输出并结束
        proc.stdout.close()
        proc.wait()
        t0 = time.perf_counter()
        final = json.loads(rec.FinalResult())
        model_infer_seconds += time.perf_counter() - t0
        print("\n最终结果:", final.get("text", ""))

        # 统计信息
        rtf = (audio_seconds / model_infer_seconds) if model_infer_seconds > 0 else float("inf")
        print("\n—— 统计 ——")
        print(f"模型加载时间: {model_load_seconds:.3f} s")
        print(f"音频时长: {audio_seconds:.3f} s")
        print(f"推理总时间: {model_infer_seconds:.3f} s")
        print(f"实时系数 RTF: {rtf:.2f}x  (>1 表示快于实时)")

    # 先尝试当作 WAV 读取
    try:
        with wave.open(audio_path, "rb") as wf:
            nch = wf.getnchannels()
            sampwidth = wf.getsampwidth()
            fr = wf.getframerate()

            if sampwidth != 2:
                # 不是 16-bit PCM，改走 ffmpeg 流程
                print("输入 WAV 非 16-bit PCM，切换为 ffmpeg 转码识别...\n")
                return recognize_from_ffmpeg(audio_path)

            rec, model_load_seconds = _init_recognizer(model_path, fr, use_grammar)

            print("开始识别...\n")
            CHUNK_FRAMES = 4000
            model_infer_seconds = 0.0
            audio_seconds = 0.0
            while True:
                data = wf.readframes(CHUNK_FRAMES)
                if not data:
                    break
                if nch > 1:
                    data = _downmix_to_mono_int16(data, nch)

                # 统计音频长度（按原通道数和采样位宽计算）
                frames_read = len(data) // (2)  # after downmix mono s16, bytes per frame=2
                audio_seconds += frames_read / fr

                t0 = time.perf_counter()
                accepted = rec.AcceptWaveform(data)
                model_infer_seconds += time.perf_counter() - t0

                if accepted:
                    result = json.loads(rec.Result())
                    print("识别结果:", result.get("text", ""))
                else:
                    if args.debug:
                        partial = json.loads(rec.PartialResult()).get("partial", "")
                        if partial:
                            print("部分结果:", partial)

            t0 = time.perf_counter()
            final = json.loads(rec.FinalResult())
            model_infer_seconds += time.perf_counter() - t0
            print("\n最终结果:", final.get("text", ""))

            # 统计信息
            rtf = (audio_seconds / model_infer_seconds) if model_infer_seconds > 0 else float("inf")
            print("\n—— 统计 ——")
            print(f"模型加载时间: {model_load_seconds:.3f} s")
            print(f"音频时长: {audio_seconds:.3f} s")
            print(f"推理总时间: {model_infer_seconds:.3f} s")
            print(f"实时系数 RTF: {rtf:.2f}x  (>1 表示快于实时)")
    except wave.Error:
        # 不是标准 WAV，使用 ffmpeg 解码
        return recognize_from_ffmpeg(audio_path)


if __name__ == "__main__":
    main()
