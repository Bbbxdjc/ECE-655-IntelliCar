import os
import sys
import json
import queue
import argparse
import sounddevice as sd
from vosk import Model, KaldiRecognizer
from parse_en import to_structured
from vosk_config import GRAMMAR_DEFAULT

# ================== Configuration (env + CLI) ==================
# Env vars (can be overridden by CLI):
# - VOSK_MODEL_PATH: absolute/relative path to model dir (default ./vosk_models/en-small)
# - VOSK_USE_GRAMMAR: 1/0 or true/false (default 1)
# - VOSK_DEVICE_INDEX: integer device index (optional)

DEFAULT_MODEL_PATH = os.environ.get("VOSK_MODEL_PATH", "./vosk_models/en-small")
DEFAULT_USE_GRAMMAR = os.environ.get("VOSK_USE_GRAMMAR", "1").lower() not in {"0", "false", "no"}
ENV_DEVICE_INDEX = os.environ.get("VOSK_DEVICE_INDEX")

# Use shared grammar configuration
GRAMMAR = GRAMMAR_DEFAULT

# Globals initialized in main()
model = None
rec_grammar = None      # Grammar 模式识别器
rec_no_grammar = None   # No-Grammar 模式识别器
q = queue.Queue()


def cb(indata, frames, time, status):
    """Audio callback function."""
    q.put(bytes(indata))


def recognize_once(timeout_s=10, device_index=None, debug=False, min_confidence=0.9):
    """
    Listen for a single voice command and return structured JSON.
    
    Args:
        timeout_s: Maximum time to listen in seconds
        device_index: Optional sounddevice input device index
        debug: If True, print detailed debug information
    
    Returns:
        Structured command dict with format:
        {
            "action": "forward|backward|left|right|stop",
            "duration_ms": int|null,
            "angle_deg": int|null,
            "speed": str|null
        }
    """
    import time
    
    if debug:
        print("\n" + "="*60)
        print("🎤 开始新的识别会话")
        print("="*60)
    
    # Clear queue to avoid stale audio frames from previous calls
    cleared = 0
    while not q.empty():
        try:
            q.get_nowait()
            cleared += 1
        except queue.Empty:
            break
    
    if debug and cleared > 0:
        print(f"🧹 清空了 {cleared} 个旧音频块")
    
    # Reset both recognizers to avoid residual effects from previous calls
    global rec_grammar, rec_no_grammar
    rec_grammar.Reset()
    rec_no_grammar.Reset()
    
    if debug:
        print("🔄 识别器状态已重置")
        print(f"⏱️  等待语音输入（超时 {timeout_s} 秒）...")
    
    start = time.time()
    audio_chunks_received = 0
    partial_results = []
    
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
                    # Use short timeout to avoid indefinite blocking
                    data = q.get(timeout=0.1)
                    audio_chunks_received += 1
                    
                    if debug and audio_chunks_received == 1:
                        print(f"📥 开始接收音频数据...")
                    
                except queue.Empty:
                    # No data yet, continue waiting until timeout_s
                    continue
                
                # 同时送给两个识别器
                grammar_done = rec_grammar.AcceptWaveform(data)
                no_grammar_done = rec_no_grammar.AcceptWaveform(data)
                
                # 当两者都完成识别时
                if grammar_done and no_grammar_done:
                    # 获取两个识别器的结果
                    grammar_result = json.loads(rec_grammar.Result())
                    no_grammar_result = json.loads(rec_no_grammar.Result())
                    
                    grammar_text = grammar_result.get("text", "")
                    no_grammar_text = no_grammar_result.get("text", "")
                    
                    # Debug: 显示两个识别器的结果
                    if debug:
                        print(f"\n🔍 双路径识别结果：")
                        print(f"  Grammar 文本: '{grammar_text}'")
                        print(f"  No-Grammar 文本: '{no_grammar_text}'")
                    
                    # 提取两个识别器的置信度信息
                    def extract_confidence(result):
                        """提取置信度详情"""
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
                    
                    # 显示两个识别器的置信度
                    if debug:
                        print(f"\n🎯 Grammar 置信度详情:")
                        if grammar_word_details:
                            for word, conf in grammar_word_details:
                                print(f"   '{word}': {conf:.3f}")
                            print(f"   平均置信度: {grammar_confidence:.3f}")
                        else:
                            print(f"   (无数据)")
                        
                        print(f"\n🎯 No-Grammar 置信度详情:")
                        if no_grammar_word_details:
                            for word, conf in no_grammar_word_details:
                                print(f"   '{word}': {conf:.3f}")
                            print(f"   平均置信度: {no_grammar_confidence:.3f}")
                        else:
                            print(f"   (无数据)")
                    
                    # 交叉验证逻辑：
                    # 只有当 No-Grammar 所有词置信度都是 1.0 时才进行交叉验证
                    # 否则直接放行使用 Grammar 结果
                    
                    # 检查 No-Grammar 是否所有词置信度都是 1.0
                    all_perfect_confidence = False
                    if no_grammar_word_details:
                        all_perfect_confidence = all(conf == 1.0 for _, conf in no_grammar_word_details)
                    
                    if debug:
                        print(f"\n🔍 交叉验证策略：")
                        if no_grammar_word_details:
                            print(f"  No-Grammar 所有词置信度都是 1.0: {all_perfect_confidence}")
                        else:
                            print(f"  No-Grammar 无置信度数据")
                    
                    # 如果 No-Grammar 所有词置信度都是 1.0，进行交叉验证
                    if all_perfect_confidence:
                        if debug:
                            print(f"  → 触发交叉验证")
                        
                        grammar_words = set(grammar_text.lower().split())
                        no_grammar_words = set(no_grammar_text.lower().split())
                        
                        is_subset = grammar_words.issubset(no_grammar_words)
                        
                        if debug:
                            print(f"  Grammar 词集: {grammar_words}")
                            print(f"  No-Grammar 词集: {no_grammar_words}")
                            print(f"  Grammar ⊆ No-Grammar: {is_subset}")
                        
                        # 如果 Grammar 的词不是 No-Grammar 的子集，返回 null
                        if not is_subset:
                            if debug:
                                missing_words = grammar_words - no_grammar_words
                                print(f"  ⚠️  Grammar 中有词不在 No-Grammar 中: {missing_words}")
                                print(f"  ❌ 交叉验证失败，返回 null")
                            return {
                                "action": None,
                                "duration_ms": None,
                                "angle_deg": None,
                                "speed": None
                            }
                        
                        if debug:
                            print(f"  ✓ 交叉验证通过")
                    else:
                        # No-Grammar 有不确定性，直接放行
                        if debug:
                            print(f"  → No-Grammar 有不确定性，跳过交叉验证，直接使用 Grammar 结果")
                    
                    if debug:
                        print(f"  ✓ 使用 Grammar 结果")
                    
                    # 使用 Grammar 结果（严格置信度检查：所有词必须 1.0）
                    result = grammar_result
                    text = grammar_text
                    
                    # 检查 Grammar 的置信度：所有词必须都是 1.0
                    grammar_all_perfect = False
                    if grammar_word_details:
                        grammar_all_perfect = all(conf == 1.0 for _, conf in grammar_word_details)
                    
                    if debug:
                        print(f"\n🔍 Grammar 置信度检查：")
                        if grammar_word_details:
                            print(f"  所有词置信度都是 1.0: {grammar_all_perfect}")
                            if not grammar_all_perfect:
                                print(f"  ⚠️  存在置信度 < 1.0 的词：")
                                for word, conf in grammar_word_details:
                                    if conf < 1.0:
                                        print(f"     '{word}': {conf:.3f}")
                        else:
                            print(f"  无置信度数据")
                    
                    # 如果 Grammar 有任何词置信度 < 1.0，直接返回 null
                    if not grammar_all_perfect:
                        if debug:
                            print(f"  ❌ Grammar 置信度不完美，返回 null")
                        return {
                            "action": None,
                            "duration_ms": None,
                            "angle_deg": None,
                            "speed": None
                        }
                    
                    if debug:
                        print(f"  ✓ Grammar 所有词置信度完美 (1.0)")

                    # Grammar 置信度完美，识别文本为空也返回 null
                    if not text or not text.strip():
                        if debug:
                            print(f"⚠️  识别文本为空，返回null")
                        return {
                            "action": None,
                            "duration_ms": None,
                            "angle_deg": None,
                            "speed": None
                        }

                    # 所有检查通过，开始解析
                    if debug:
                        elapsed = time.time() - start
                        print(f"\n✅ Vosk 识别完成 (耗时 {elapsed:.2f}s)")
                        print(f"📊 接收了 {audio_chunks_received} 个音频块 (~{audio_chunks_received*0.5:.1f}秒音频)")
                        print(f"📝 识别文本: '{text}'")
                        print(f"\n🔍 开始解析文本...")
                    
                    result = to_structured(text, debug=debug)
                    return result
                else:
                    # Partial result (ongoing recognition) - 只显示 No-Grammar 的部分结果
                    partial = json.loads(rec_no_grammar.PartialResult()).get("partial", "")
                    if partial and partial not in partial_results:
                        partial_results.append(partial)
                        if debug:
                            print(f"  💭 部分识别: '{partial}'")
            
            if debug:
                print(f"\n⏰ 超时 ({timeout_s}秒)")
                if audio_chunks_received == 0:
                    print("⚠️  没有接收到任何音频数据")
                    print("   可能原因：麦克风未连接或权限未授予")
                elif audio_chunks_received > 0:
                    print(f"   接收了 {audio_chunks_received} 个音频块，但未识别到完整语音")
                    if partial_results:
                        print(f"   部分识别: {partial_results}")
                        print("   ⚠️  部分识别无置信度信息，不使用")
                        # 部分识别没有置信度，不使用以保证质量
                    print("   可能原因：音量太低、噪音太大、或说话后未保持安静")
    except sd.PortAudioError as e:
        print(f"Audio device error: {e}", file=sys.stderr)
        print("Check microphone connection and permissions.", file=sys.stderr)
        print("List devices: python -c 'import sounddevice as sd; print(sd.query_devices())'", file=sys.stderr)
        return {
            "action": None,
            "duration_ms": None,
            "angle_deg": None,
            "speed": None
        }
    except Exception as e:
        print(f"Unexpected error during recognition: {e}", file=sys.stderr)
        return {
            "action": None,
            "duration_ms": None,
            "angle_deg": None,
            "speed": None
        }
    
    # Timeout: return stop command
    return {
        "action": None,
        "duration_ms": None,
        "angle_deg": None,
        "speed": None
    }

def _init_recognizer(model_path: str, use_grammar: bool):
    """Initialize dual-path recognizers with error handling."""
    global model, rec_grammar, rec_no_grammar
    try:
        model = Model(model_path)
    except Exception as e:
        print(f"Failed to load Vosk model from: {model_path}", file=sys.stderr)
        print(f"Error: {e}", file=sys.stderr)
        print("Please check:", file=sys.stderr)
        print("  1. Path is correct and model files exist", file=sys.stderr)
        print("  2. Model directory contains: am/, conf/, graph/, ivector/", file=sys.stderr)
        print("  3. Download model: https://alphacephei.com/vosk/models", file=sys.stderr)
        sys.exit(1)

    try:
        # 始终初始化两个识别器进行双路径验证
        rec_grammar = KaldiRecognizer(model, 16000, json.dumps(GRAMMAR))
        rec_grammar.SetWords(True)
        
        rec_no_grammar = KaldiRecognizer(model, 16000)
        rec_no_grammar.SetWords(True)
        
        if use_grammar:
            print("Dual-path mode: Grammar + No-Grammar (cross-validation)")
        
    except Exception as e:
        print(f"Failed to initialize recognizers: {e}", file=sys.stderr)
        sys.exit(1)


def _parse_args():
    parser = argparse.ArgumentParser(description="Vosk local recognizer (unified JSON output)")
    parser.add_argument("--model", default=DEFAULT_MODEL_PATH, help="Path to Vosk model directory")
    parser.add_argument("--device", type=int, default=None, help="Input device index (sounddevice)")
    parser.add_argument("--no-grammar", action="store_true", help="Disable grammar constraints")
    parser.add_argument("--timeout", type=int, default=10, help="Listen timeout per attempt (seconds)")
    parser.add_argument("--debug", action="store_true", help="Enable detailed debug output")
    parser.add_argument("--min-confidence", type=float, default=0.9, help="Minimum confidence threshold (0.0-1.0)")
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()

    # Resolve final config (env provides defaults, CLI overrides)
    model_path = args.model
    use_grammar = not args.no_grammar if args.no_grammar else DEFAULT_USE_GRAMMAR
    if args.device is not None:
        device_index = args.device
    else:
        device_index = int(ENV_DEVICE_INDEX) if ENV_DEVICE_INDEX and ENV_DEVICE_INDEX.isdigit() else None

    print("Starting Vosk local recognizer")
    print(f"  Model: {model_path}")
    print(f"  Grammar: {'ON' if use_grammar else 'OFF'}")
    print(f"  Device: {device_index if device_index is not None else '(default)'}")
    print(f"  Debug: {'ON' if args.debug else 'OFF'}")
    print("Examples: 'forward five seconds', 'turn left ninety degrees', 'stop'\n")

    _init_recognizer(model_path, use_grammar)

    try:
        while True:
            if not args.debug:
                print("Listening for command...")
            
            task = recognize_once(timeout_s=args.timeout, device_index=device_index, debug=args.debug, min_confidence=args.min_confidence)

            # Provide clear feedback based on result
            if not args.debug:
                if task['action'] == 'stop' and all(v is None for k, v in task.items() if k != 'action'):
                    # All fields null + stop action = timeout/no speech
                    print(f"[Timeout] No command recognized in {args.timeout}s")
                else:
                    print("[Recognized]")

            print("\n" + "="*60)
            print("📤 最终输出 JSON:")
            print("="*60)
            print(json.dumps(task, ensure_ascii=False, indent=2))
            print("="*60 + "\n")
    except KeyboardInterrupt:
        print("\nShutting down gracefully...")
        sys.exit(0)

