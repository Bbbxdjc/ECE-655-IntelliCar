#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
macOS 实时语音命令系统（模块化版本）
集成 VAD、STT 和 LLM 三个模块
"""

import sys
import json
import signal
import argparse
import threading
import queue

from vad_module import AudioVAD, VADConfig
from stt_module import SpeechToText, STTConfig
from llm_module import CommandParser, LLMConfig


class RealtimeCommandSystem:
    """实时命令系统"""
    
    def __init__(self, device_idx=None, print_stt=True):
        # 初始化三个模块
        self.vad = AudioVAD(VADConfig(), device_idx=device_idx)
        self.stt = SpeechToText(STTConfig())
        self.llm = CommandParser(LLMConfig())
        
        self.print_stt = print_stt
        self.segments_queue = queue.Queue()
        self.stop_event = threading.Event()
    
    def _on_segment_detected(self, seg_id: int, audio_data):
        """VAD 检测到语音段的回调"""
        self.segments_queue.put((seg_id, audio_data))
    
    def _consumer_worker(self):
        """消费者线程：处理语音段"""
        while not self.stop_event.is_set():
            try:
                seg_id, audio_data = self.segments_queue.get(timeout=0.1)
            except queue.Empty:
                continue
            
            try:
                # 步骤 1: STT 语音识别
                text = self.stt.transcribe(audio_data)
                
                if self.print_stt:
                    print(f"👤 STT (segment #{seg_id}): {text or '(empty)'}")
                
                if not text:
                    self.segments_queue.task_done()
                    continue
                
                # 步骤 2: LLM 解析命令
                commands = self.llm.parse(text)
                
                # 步骤 3: 输出命令（添加 segment id）
                for cmd in commands:
                    cmd["id"] = seg_id
                    print(json.dumps(cmd, ensure_ascii=False, separators=(",", ":")))
                
            except Exception as e:
                print(f"❌ Processing failed (segment #{seg_id}): {e}", file=sys.stderr)
            
            finally:
                self.segments_queue.task_done()
    
    def start(self):
        """启动实时命令系统"""
        print("🎙️  macOS real-time command mode (modular version)")
        print(f"   - VAD: RMS-based endpoint detection")
        print(f"   - STT: Faster-Whisper")
        print(f"   - LLM: Structured command parsing")
        print("Press Ctrl+C to stop.\n")
        
        # 启动消费者线程
        consumer_thread = threading.Thread(target=self._consumer_worker, daemon=True)
        consumer_thread.start()
        
        # 设置信号处理
        def signal_handler(sig, frame):
            self.stop()
        
        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)
        
        try:
            # 启动 VAD（阻塞）
            self.vad.start(callback=self._on_segment_detected)
        except KeyboardInterrupt:
            pass
        finally:
            self.stop()
            consumer_thread.join(timeout=1.0)
    
    def stop(self):
        """停止系统"""
        self.stop_event.set()
        self.vad.stop()


def main():
    parser = argparse.ArgumentParser(description="macOS realtime voice → modular command system")
    parser.add_argument("--device", type=int, default=None, help="sounddevice input device index")
    parser.add_argument("--print-stt", action="store_true", help="print STT text")
    args = parser.parse_args()
    
    if args.device is None:
        print("ℹ️  Tip: run `python -c 'import sounddevice as sd; print(sd.query_devices())'` "
              "to find your mic index.", file=sys.stderr)
    
    try:
        system = RealtimeCommandSystem(device_idx=args.device, print_stt=args.print_stt)
        system.start()
    except Exception as e:
        print(f"❌ System error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
