#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
语音识别（STT）模块
使用 Faster-Whisper 进行语音转文字
"""

import os
import threading
from typing import Optional

import numpy as np
from faster_whisper import WhisperModel


class STTConfig:
    """STT 配置参数"""
    def __init__(self):
        self.model_size = os.environ.get("WHISPER_MODEL", "tiny")
        self.device = os.environ.get("WHISPER_DEV", "cpu")
        self.compute_type = os.environ.get("WHISPER_CT", "int8")
        self.language = None  # None 表示自动检测，可设置 "zh" 或 "en"


class SpeechToText:
    """语音识别服务"""
    
    def __init__(self, config: STTConfig = None):
        self.config = config or STTConfig()
        self.model: Optional[WhisperModel] = None
        self.lock = threading.Lock()
    
    def _ensure_model(self):
        """延迟加载模型"""
        if self.model is None:
            self.model = WhisperModel(
                self.config.model_size,
                device=self.config.device,
                compute_type=self.config.compute_type
            )
    
    def transcribe(self, audio_data: np.ndarray) -> str:
        """
        将音频转换为文本
        
        Args:
            audio_data: float32 格式的音频数据
            
        Returns:
            识别的文本
        """
        with self.lock:
            self._ensure_model()
            
            segments, _ = self.model.transcribe(
                audio_data,
                language=self.config.language,
                without_timestamps=True,
                vad_filter=False
            )
            
            text = "".join(seg.text for seg in segments).strip()
            return text
