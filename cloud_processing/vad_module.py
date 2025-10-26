#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
实时音频采集 + VAD (Voice Activity Detection)
使用 RMS 能量检测进行端点检测
"""

import threading
import queue
from collections import deque
from typing import Optional, Callable

import numpy as np
import sounddevice as sd


class VADConfig:
    """VAD 配置参数"""
    def __init__(self):
        self.sample_rate = 16000
        self.channels = 1
        self.frame_ms = 20
        self.max_seg_sec = 20.0
        
        # RMS VAD 参数
        self.rms_win_ms = 200
        self.rms_thresh_db = -35.0
        self.rms_hang_ms_on = 120      # 连续多久的语音才触发"开始"
        self.rms_hang_ms_off = 450     # 连续多久的静音才触发"结束"
        self.rms_lookback_ms = 500     # 回溯窗口：检测到语音后往前看多久
        self.rms_floor_decay = 0.995


class AudioVAD:
    """实时音频采集和语音活动检测"""
    
    def __init__(self, config: VADConfig = None, device_idx: Optional[int] = None):
        self.config = config or VADConfig()
        self.device_idx = device_idx
        self.stop_event = threading.Event()
        
        # 计算帧参数
        self.frame_samples = self.config.sample_rate * self.config.frame_ms // 1000
        
        # 状态变量
        self.segment_counter = 0
        self.counter_lock = threading.Lock()
        
    def _select_device(self):
        """选择音频输入设备"""
        if self.device_idx is not None:
            return self.device_idx
            
        try:
            default_in = sd.default.device[0]
        except Exception:
            default_in = None
            
        devs = sd.query_devices()
        if default_in is None or devs[default_in]['max_input_channels'] < 1:
            alt = next((i for i, d in enumerate(devs) if d['max_input_channels'] >= 1), None)
            if alt is None:
                raise RuntimeError("❌ No input-capable audio device found.")
            print(f"ℹ️  Selected input device #{alt}: {devs[alt]['name']}")
            return alt
        return default_in
    
    def _pcm_float_to_db(self, frame: np.ndarray) -> float:
        """将 PCM float 数据转换为 dB"""
        rms = np.sqrt(np.mean(np.square(frame), dtype=np.float64) + 1e-8)
        return 20.0 * np.log10(rms + 1e-8)
    
    def start(self, callback: Callable[[int, np.ndarray], None]):
        """
        开始音频采集和 VAD 检测
        
        Args:
            callback: 回调函数，接收 (segment_id, audio_data)
        """
        device_idx = self._select_device()
        
        # VAD 状态
        ring_sil = deque(maxlen=self.config.rms_hang_ms_off // self.config.frame_ms)
        ring_vox = deque(maxlen=self.config.rms_hang_ms_on // self.config.frame_ms)
        # 使用独立的回溯窗口，避免丢失语音开头
        recent_frames = deque(maxlen=self.config.rms_lookback_ms // self.config.frame_ms)
        
        in_speech = False
        seg_frames = []
        seg_start_t = None
        noise_floor_db = -60.0
        
        def on_audio(indata, frames, time_info, status):
            nonlocal in_speech, seg_frames, seg_start_t, noise_floor_db
            
            if self.stop_event.is_set():
                raise sd.CallbackStop()
            
            mono = np.asarray(indata[:, 0], dtype=np.float32)
            total = len(mono)
            idx = 0
            
            while idx + self.frame_samples <= total:
                frame = mono[idx:idx + self.frame_samples]
                idx += self.frame_samples
                
                db = self._pcm_float_to_db(frame)
                
                # 动态噪声底更新
                if not in_speech:
                    noise_floor_db = max(-80.0, min(-5.0, 
                        self.config.rms_floor_decay * noise_floor_db + 
                        (1.0 - self.config.rms_floor_decay) * db))
                
                dynamic_thresh = max(noise_floor_db + 10.0, self.config.rms_thresh_db)
                is_voiced = db >= dynamic_thresh
                
                ring_vox.append(is_voiced)
                ring_sil.append(not is_voiced)
                
                # 保留最近的帧用于回溯
                if not in_speech:
                    recent_frames.append(frame.astype(np.float32))
                
                # 检测语音开始
                if not in_speech and len(ring_vox) == ring_vox.maxlen and sum(ring_vox) == len(ring_vox):
                    in_speech = True
                    seg_frames = list(recent_frames)
                    import time
                    seg_start_t = time.time() - (len(recent_frames) * self.config.frame_ms / 1000.0)
                
                # 收集语音帧
                if in_speech:
                    seg_frames.append(frame.astype(np.float32))
                    
                    import time
                    too_long = seg_start_t and (time.time() - seg_start_t) >= self.config.max_seg_sec
                    long_sil = len(ring_sil) == ring_sil.maxlen and sum(ring_sil) == len(ring_sil)
                    
                    if long_sil or too_long:
                        # 完成一段语音
                        if seg_frames:
                            pcm_f32 = np.concatenate(seg_frames, axis=0)
                            with self.counter_lock:
                                self.segment_counter += 1
                                seg_id = self.segment_counter
                            callback(seg_id, pcm_f32)
                        
                        # 重置状态
                        in_speech = False
                        seg_frames = []
                        seg_start_t = None
                        ring_sil.clear()
                        ring_vox.clear()
                        recent_frames.clear()
        
        # 启动音频流
        with sd.InputStream(
            samplerate=self.config.sample_rate,
            device=device_idx,
            channels=self.config.channels,
            dtype="float32",
            blocksize=self.frame_samples,
            callback=on_audio,
            latency="low"
        ):
            while not self.stop_event.is_set():
                sd.sleep(50)
    
    def stop(self):
        """停止音频采集"""
        self.stop_event.set()
