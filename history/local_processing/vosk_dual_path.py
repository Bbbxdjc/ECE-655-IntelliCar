#!/usr/bin/env python3
"""
Vosk Dual-Path Recognizer
同时运行 Grammar 和 No-Grammar 模式，对比结果
"""

import os
import sys
import json
import queue
import time
import argparse
import sounddevice as sd
from vosk import Model, KaldiRecognizer
from parse_en import to_structured
from vosk_config import GRAMMAR_DEFAULT

# Configuration
DEFAULT_MODEL_PATH = os.environ.get("VOSK_MODEL_PATH", "./vosk_models/en-small")
ENV_DEVICE_INDEX = os.environ.get("VOSK_DEVICE_INDEX")

# Globals
model = None
rec_grammar = None      # Grammar 模式识别器
rec_no_grammar = None   # No-Grammar 模式识别器
q = queue.Queue()


def cb(indata, frames, time, status):
    """Audio callback function."""
    q.put(bytes(indata))


def recognize_dual_path(timeout_s=10, device_index=None, min_confidence=0.9):
    """
    使用双路径同时识别，返回两个结果进行对比。
    
    Returns:
        dict: {
            "grammar": {...},
            "no_grammar": {...},
            "comparison": {...}
        }
    """
    print("\n" + "="*80)
    print("🎤 双路径识别开始")
    print("="*80)
    
    # Clear queue
    while not q.empty():
        try:
            q.get_nowait()
        except queue.Empty:
            break
    
    # Reset both recognizers
    rec_grammar.Reset()
    rec_no_grammar.Reset()
    
    print(f"⏱️  等待语音输入（超时 {timeout_s} 秒）...\n")
    
    start = time.time()
    audio_chunks = []
    audio_chunks_received = 0
    
    try:
        with sd.RawInputStream(
            samplerate=16000,
            blocksize=8000,
            dtype='int16',
            channels=1,
            callback=cb,
            device=device_index
        ):
            while time.time() - start < timeout_s:
                try:
                    data = q.get(timeout=0.1)
                    audio_chunks.append(data)
                    audio_chunks_received += 1
                    
                except queue.Empty:
                    continue
                
                # 同时送给两个识别器
                grammar_done = rec_grammar.AcceptWaveform(data)
                no_grammar_done = rec_no_grammar.AcceptWaveform(data)
                
                # 如果两者都完成了，可以提前返回
                if grammar_done and no_grammar_done:
                    elapsed = time.time() - start
                    return process_results(
                        rec_grammar, rec_no_grammar, 
                        elapsed, audio_chunks_received, min_confidence
                    )
            
            # Timeout - get final results
            elapsed = time.time() - start
            return process_results(
                rec_grammar, rec_no_grammar,
                elapsed, audio_chunks_received, min_confidence
            )
            
    except Exception as e:
        print(f"❌ 识别错误: {e}", file=sys.stderr)
        return None


def extract_filtered_result(recognizer, min_confidence):
    """从识别器提取结果并过滤低置信度词"""
    result = json.loads(recognizer.Result())
    text = result.get("text", "")
    
    filtered_words = []
    word_details = []
    confidence = None
    
    if "result" in result and result["result"]:
        for word_info in result["result"]:
            word_text = word_info.get("word", "")
            word_conf = word_info.get("conf", 0)
            word_details.append((word_text, word_conf))
            
            if word_conf >= min_confidence:
                filtered_words.append(word_text)
        
        confidences = [w[1] for w in word_details]
        if confidences:
            confidence = sum(confidences) / len(confidences)
    
    filtered_text = " ".join(filtered_words) if filtered_words else ""
    
    return {
        "original_text": text,
        "filtered_text": filtered_text,
        "word_details": word_details,
        "confidence": confidence,
        "filtered_count": len(word_details) - len(filtered_words)
    }


def process_results(rec_grammar, rec_no_grammar, elapsed, audio_chunks, min_confidence):
    """处理并对比两个识别器的结果"""
    
    print(f"✅ 识别完成 (耗时 {elapsed:.2f}s, 音频块 {audio_chunks})")
    print("="*80)
    
    # 提取结果
    grammar_result = extract_filtered_result(rec_grammar, min_confidence)
    no_grammar_result = extract_filtered_result(rec_no_grammar, min_confidence)
    
    # 显示 Grammar 模式结果
    print("\n📊 Grammar 模式结果:")
    print("-" * 80)
    print(f"原始文本: '{grammar_result['original_text']}'")
    
    if grammar_result['word_details']:
        print(f"置信度详情:")
        for word, conf in grammar_result['word_details']:
            status = "✓" if conf >= min_confidence else "✗"
            print(f"  {status} '{word}': {conf:.3f}")
        print(f"平均置信度: {grammar_result['confidence']:.3f}")
        
        if grammar_result['filtered_count'] > 0:
            print(f"过滤掉 {grammar_result['filtered_count']} 个低置信度词")
            print(f"过滤后文本: '{grammar_result['filtered_text']}'")
    else:
        print("(无详细数据)")
    
    # 解析 Grammar 结果
    if grammar_result['filtered_text']:
        grammar_json = to_structured(grammar_result['filtered_text'])
    else:
        grammar_json = {"action": None, "duration_ms": None, "angle_deg": None, "speed": None}
    
    print(f"\nJSON 输出:")
    print(json.dumps(grammar_json, indent=2, ensure_ascii=False))
    
    # 显示 No-Grammar 模式结果
    print("\n📊 No-Grammar 模式结果:")
    print("-" * 80)
    print(f"原始文本: '{no_grammar_result['original_text']}'")
    
    if no_grammar_result['word_details']:
        print(f"置信度详情:")
        for word, conf in no_grammar_result['word_details']:
            status = "✓" if conf >= min_confidence else "✗"
            print(f"  {status} '{word}': {conf:.3f}")
        print(f"平均置信度: {no_grammar_result['confidence']:.3f}")
        
        if no_grammar_result['filtered_count'] > 0:
            print(f"过滤掉 {no_grammar_result['filtered_count']} 个低置信度词")
            print(f"过滤后文本: '{no_grammar_result['filtered_text']}'")
    else:
        print("(无详细数据)")
    
    # 解析 No-Grammar 结果
    if no_grammar_result['filtered_text']:
        no_grammar_json = to_structured(no_grammar_result['filtered_text'])
    else:
        no_grammar_json = {"action": None, "duration_ms": None, "angle_deg": None, "speed": None}
    
    print(f"\nJSON 输出:")
    print(json.dumps(no_grammar_json, indent=2, ensure_ascii=False))
    
    # 对比分析
    print("\n📊 对比分析:")
    print("-" * 80)
    
    # 文本对比
    if grammar_result['original_text'] == no_grammar_result['original_text']:
        print("✓ 原始文本相同")
    else:
        print("✗ 原始文本不同:")
        print(f"  Grammar:    '{grammar_result['original_text']}'")
        print(f"  No-Grammar: '{no_grammar_result['original_text']}'")
    
    # 过滤后文本对比
    if grammar_result['filtered_text'] == no_grammar_result['filtered_text']:
        print("✓ 过滤后文本相同")
    else:
        print("✗ 过滤后文本不同:")
        print(f"  Grammar:    '{grammar_result['filtered_text']}'")
        print(f"  No-Grammar: '{no_grammar_result['filtered_text']}'")
    
    # JSON 对比
    if grammar_json == no_grammar_json:
        print("✓ JSON 输出相同")
    else:
        print("✗ JSON 输出不同:")
        print(f"  Grammar:    {grammar_json}")
        print(f"  No-Grammar: {no_grammar_json}")
    
    # 置信度对比
    if grammar_result['confidence'] and no_grammar_result['confidence']:
        diff = abs(grammar_result['confidence'] - no_grammar_result['confidence'])
        print(f"\n置信度差异: {diff:.3f}")
        if grammar_result['confidence'] > no_grammar_result['confidence']:
            print(f"  Grammar 更高 ({grammar_result['confidence']:.3f} vs {no_grammar_result['confidence']:.3f})")
        else:
            print(f"  No-Grammar 更高 ({no_grammar_result['confidence']:.3f} vs {grammar_result['confidence']:.3f})")
    
    print("="*80)
    
    return {
        "grammar": {
            "result": grammar_result,
            "json": grammar_json
        },
        "no_grammar": {
            "result": no_grammar_result,
            "json": no_grammar_json
        },
        "elapsed": elapsed,
        "audio_chunks": audio_chunks
    }


def _init_dual_recognizers(model_path: str):
    """初始化双路径识别器"""
    global model, rec_grammar, rec_no_grammar
    
    print("初始化双路径识别器...")
    
    try:
        model = Model(model_path)
        print(f"✓ 模型加载成功: {model_path}")
    except Exception as e:
        print(f"❌ 模型加载失败: {e}", file=sys.stderr)
        sys.exit(1)
    
    try:
        # Grammar 模式识别器
        rec_grammar = KaldiRecognizer(model, 16000, json.dumps(GRAMMAR_DEFAULT))
        rec_grammar.SetWords(True)
        print(f"✓ Grammar 识别器已初始化 (词汇表: {len(GRAMMAR_DEFAULT)} 个)")
        
        # No-Grammar 模式识别器
        rec_no_grammar = KaldiRecognizer(model, 16000)
        rec_no_grammar.SetWords(True)
        print(f"✓ No-Grammar 识别器已初始化")
        
    except Exception as e:
        print(f"❌ 识别器初始化失败: {e}", file=sys.stderr)
        sys.exit(1)


def _parse_args():
    parser = argparse.ArgumentParser(description="Vosk dual-path recognizer (Grammar vs No-Grammar)")
    parser.add_argument("--model", default=DEFAULT_MODEL_PATH, help="Path to Vosk model directory")
    parser.add_argument("--device", type=int, default=None, help="Input device index")
    parser.add_argument("--timeout", type=int, default=10, help="Listen timeout (seconds)")
    parser.add_argument("--min-confidence", type=float, default=0.9, help="Minimum confidence (0.0-1.0)")
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    
    device_index = args.device
    if device_index is None and ENV_DEVICE_INDEX and ENV_DEVICE_INDEX.isdigit():
        device_index = int(ENV_DEVICE_INDEX)
    
    print("="*80)
    print("Vosk 双路径识别器 (Grammar vs No-Grammar)")
    print("="*80)
    print(f"模型: {args.model}")
    print(f"设备: {device_index if device_index is not None else '(默认)'}")
    print(f"超时: {args.timeout}s")
    print(f"置信度阈值: {args.min_confidence}")
    print("="*80)
    
    _init_dual_recognizers(args.model)
    
    try:
        while True:
            result = recognize_dual_path(
                timeout_s=args.timeout,
                device_index=device_index,
                min_confidence=args.min_confidence
            )
            
            if result is None:
                continue
            
            print("\n按 Ctrl+C 退出，或继续说话进行下一次识别...\n")
            
    except KeyboardInterrupt:
        print("\n\n👋 程序退出")
        sys.exit(0)

