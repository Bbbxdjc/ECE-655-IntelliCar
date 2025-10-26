#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Vosk Realtime Continuous Recognition
连续实时识别多个语音命令，类似 VAD 的 realtime 模式
"""

import os
import sys
import json
import queue
import argparse
import threading
import numpy as np
import sounddevice as sd
from collections import deque
from vosk import Model, KaldiRecognizer
from parse_en import to_structured
from vosk_config import GRAMMAR_DEFAULT

# ================== Configuration ==================
DEFAULT_MODEL_PATH = os.environ.get("VOSK_MODEL_PATH", "./vosk_models/en-small")
DEFAULT_USE_GRAMMAR = os.environ.get("VOSK_USE_GRAMMAR", "1").lower() not in {"0", "false", "no"}
ENV_DEVICE_INDEX = os.environ.get("VOSK_DEVICE_INDEX")

GRAMMAR = GRAMMAR_DEFAULT

# VAD 参数（用于预过滤静音）
VAD_ENERGY_THRESHOLD = -40.0  # dB，低于此值认为是静音
VAD_HISTORY_FRAMES = 10       # 保留最近 N 帧用于能量平均判断
VAD_LOOKBACK_FRAMES = 15      # 回溯窗口：检测到语音后往前看多少帧（约 300ms）

# Globals
model = None
rec_grammar = None
rec_no_grammar = None
audio_queue = queue.Queue()
result_queue = queue.Queue()
stop_event = threading.Event()
segment_counter = 0
counter_lock = threading.Lock()

# VAD 状态
energy_history = deque(maxlen=VAD_HISTORY_FRAMES)
audio_buffer = deque(maxlen=VAD_LOOKBACK_FRAMES)  # 回溯缓冲区
is_speaking = False


def calculate_energy_db(audio_bytes):
    """计算音频能量（dB）"""
    audio_array = np.frombuffer(audio_bytes, dtype=np.int16).astype(np.float32) / 32768.0
    rms = np.sqrt(np.mean(np.square(audio_array)) + 1e-8)
    return 20.0 * np.log10(rms + 1e-8)


def audio_callback(indata, frames, time, status):
    """音频输入回调，添加 VAD 预过滤 + 回溯机制"""
    global is_speaking, energy_history, audio_buffer
    
    if stop_event.is_set():
        return
    
    audio_bytes = bytes(indata)
    energy_db = calculate_energy_db(audio_bytes)
    energy_history.append(energy_db)
    
    # 判断是否有语音活动
    if len(energy_history) == energy_history.maxlen:
        avg_energy = sum(energy_history) / len(energy_history)
        has_voice = avg_energy > VAD_ENERGY_THRESHOLD
        
        if not is_speaking:
            # 静音期间：保存到回溯缓冲区
            audio_buffer.append(audio_bytes)
            
            if has_voice:
                # 语音刚开始：回溯发送缓冲区中的所有帧
                for buffered_audio in audio_buffer:
                    audio_queue.put(buffered_audio)
                audio_buffer.clear()  # 清空缓冲区
                is_speaking = True
        else:
            # 语音持续中：直接发送当前帧
            audio_queue.put(audio_bytes)
            is_speaking = has_voice  # 更新状态（可能结束）


def recognition_worker(debug=False):
    """
    识别线程：持续处理音频流，检测并识别多个语音段
    现在添加了 VAD 预过滤，只处理有语音的音频
    """
    global rec_grammar, rec_no_grammar, segment_counter
    
    if debug:
        print("🎙️  识别线程启动，等待语音输入...")
        print(f"    VAD 能量阈值: {VAD_ENERGY_THRESHOLD} dB")
    
    while not stop_event.is_set():
        try:
            # 获取音频块（已经过 VAD 预过滤）
            data = audio_queue.get(timeout=0.1)
        except queue.Empty:
            continue
        
        # 同时送给两个识别器
        grammar_done = rec_grammar.AcceptWaveform(data)
        no_grammar_done = rec_no_grammar.AcceptWaveform(data)
        
        # 当两者都完成识别时（检测到语音结束）
        if grammar_done and no_grammar_done:
            with counter_lock:
                segment_counter += 1
                seg_id = segment_counter
            
            # 获取两个识别器的结果
            grammar_result = json.loads(rec_grammar.Result())
            no_grammar_result = json.loads(rec_no_grammar.Result())
            
            grammar_text = grammar_result.get("text", "")
            no_grammar_text = no_grammar_result.get("text", "")
            
            if debug:
                print(f"\n{'='*60}")
                print(f"🎤 检测到语音段 #{seg_id}")
                print(f"{'='*60}")
                print(f"  Grammar 文本: '{grammar_text}'")
                print(f"  No-Grammar 文本: '{no_grammar_text}'")
            
            # 提取置信度
            def extract_confidence(result):
                word_details = []
                if "result" in result and result["result"]:
                    for word_info in result["result"]:
                        word_text = word_info.get("word", "")
                        word_conf = word_info.get("conf", 0)
                        word_details.append((word_text, word_conf))
                
                if word_details:
                    avg_conf = sum(w[1] for w in word_details) / len(word_details)
                    return word_details, avg_conf
                return [], None
            
            grammar_word_details, grammar_confidence = extract_confidence(grammar_result)
            no_grammar_word_details, no_grammar_confidence = extract_confidence(no_grammar_result)
            
            if debug:
                print(f"\n🎯 Grammar 置信度:")
                if grammar_word_details:
                    for word, conf in grammar_word_details:
                        print(f"   '{word}': {conf:.3f}")
                    print(f"   平均: {grammar_confidence:.3f}")
                
                print(f"\n🎯 No-Grammar 置信度:")
                if no_grammar_word_details:
                    for word, conf in no_grammar_word_details:
                        print(f"   '{word}': {conf:.3f}")
                    print(f"   平均: {no_grammar_confidence:.3f}")
            
            # 交叉验证逻辑
            all_perfect_confidence = False
            if no_grammar_word_details:
                all_perfect_confidence = all(conf == 1.0 for _, conf in no_grammar_word_details)
            
            if debug:
                print(f"\n🔍 交叉验证:")
                print(f"  No-Grammar 完美置信度: {all_perfect_confidence}")
            
            # 验证逻辑
            if all_perfect_confidence:
                grammar_words = set(grammar_text.lower().split())
                no_grammar_words = set(no_grammar_text.lower().split())
                is_subset = grammar_words.issubset(no_grammar_words)
                
                if debug:
                    print(f"  Grammar ⊆ No-Grammar: {is_subset}")
                
                if not is_subset:
                    if debug:
                        missing_words = grammar_words - no_grammar_words
                        print(f"  ⚠️  验证失败，丢弃: {missing_words}")
                    continue
            
            # Grammar 置信度检查
            grammar_all_perfect = False
            if grammar_word_details:
                grammar_all_perfect = all(conf == 1.0 for _, conf in grammar_word_details)
            
            if not grammar_all_perfect:
                if debug:
                    print(f"  ⚠️  Grammar 置信度不完美，丢弃")
                continue
            
            # 文本为空检查
            if not grammar_text or not grammar_text.strip():
                if debug:
                    print(f"  ⚠️  识别文本为空，丢弃")
                continue
            
            # 解析命令
            if debug:
                print(f"\n✅ 识别文本: '{grammar_text}'")
                print(f"🔍 开始解析...")
            
            try:
                command = to_structured(grammar_text, debug=debug)
                command["id"] = seg_id
                result_queue.put(command)
                
                if debug:
                    print(f"✓ 命令已加入队列")
            except Exception as e:
                if debug:
                    print(f"❌ 解析失败: {e}")
        else:
            # 部分结果（可选显示）
            if debug:
                partial = json.loads(rec_no_grammar.PartialResult()).get("partial", "")
                if partial:
                    # 只在终端宽度允许的情况下显示
                    print(f"\r💭 {partial:<50}", end="", flush=True)


def output_worker(debug=False):
    """输出线程：持续输出识别到的命令"""
    while not stop_event.is_set():
        try:
            command = result_queue.get(timeout=0.1)
            
            # 清除部分结果显示（如果有）
            if debug:
                print("\r" + " " * 60 + "\r", end="", flush=True)
            
            print(f"\n{'='*60}")
            print(f"📤 命令 #{command.get('id', '?')}")
            print(f"{'='*60}")
            print(json.dumps(command, ensure_ascii=False, indent=2))
            print(f"{'='*60}\n")
        except queue.Empty:
            continue


def _init_recognizer(model_path: str):
    """初始化识别器"""
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
        
        print("Dual-path mode: Grammar + No-Grammar (cross-validation)")
    except Exception as e:
        print(f"Failed to initialize recognizers: {e}", file=sys.stderr)
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(
        description="Vosk Realtime Continuous Recognition (like VAD realtime mode)"
    )
    parser.add_argument("--model", default=DEFAULT_MODEL_PATH, help="Path to Vosk model directory")
    parser.add_argument("--device", type=int, default=None, help="Input device index")
    parser.add_argument("--debug", action="store_true", help="Enable detailed debug output")
    args = parser.parse_args()
    
    # 解析设备索引
    if args.device is not None:
        device_index = args.device
    else:
        device_index = int(ENV_DEVICE_INDEX) if ENV_DEVICE_INDEX and ENV_DEVICE_INDEX.isdigit() else None
    
    print("🎙️  Vosk Realtime Continuous Recognition")
    print(f"  Model: {args.model}")
    print(f"  Device: {device_index if device_index is not None else '(default)'}")
    print(f"  Debug: {'ON' if args.debug else 'OFF'}")
    print(f"  VAD Threshold: {VAD_ENERGY_THRESHOLD} dB")
    print("\n✨ 连续实时模式 + VAD 预过滤：")
    print("   - 静音期间不送入 Vosk（避免空识别）")
    print("   - 说完第一个命令，停顿 0.5 秒")
    print("   - 继续说第二个命令，停顿 0.5 秒")
    print("   - 所有命令都会被处理！")
    print("\nPress Ctrl+C to stop.\n")
    
    # 初始化识别器
    _init_recognizer(args.model)
    
    # 启动识别线程
    recognition_thread = threading.Thread(
        target=recognition_worker,
        kwargs={"debug": args.debug},
        daemon=True
    )
    recognition_thread.start()
    
    # 启动输出线程
    output_thread = threading.Thread(
        target=output_worker,
        kwargs={"debug": args.debug},
        daemon=True
    )
    output_thread.start()
    
    try:
        # 启动音频流（持续录音）
        with sd.RawInputStream(
            samplerate=16000,
            blocksize=8000,
            dtype='int16',
            channels=1,
            callback=audio_callback,
            device=device_index
        ):
            print("🔴 录音中... (说话时会自动检测并识别)\n")
            
            # 保持运行
            while True:
                sd.sleep(100)
    
    except KeyboardInterrupt:
        print("\n\n⏹️  停止录音...")
        stop_event.set()
        recognition_thread.join(timeout=1.0)
        output_thread.join(timeout=1.0)
        print("✓ 已退出")
        sys.exit(0)


if __name__ == "__main__":
    main()
