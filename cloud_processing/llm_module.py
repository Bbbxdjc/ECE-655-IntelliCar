#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
大模型命令解析模块
使用 OpenAI API 将文本解析为结构化的小车控制命令
"""

import os
import sys
import json
from typing import Dict, Any, List

from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()


class LLMConfig:
    """大模型配置参数"""
    def __init__(self):
        self.endpoint = os.environ.get("OPENAI_BASE_URL", "https://models.github.ai/inference")
        self.model_name = os.environ.get("OPENAI_MODEL", "openai/gpt-4o-mini")
        self.api_key = os.environ.get("GITHUB_TOKEN", "")
        
        # 业务约束
        self.actions = {"forward", "backward", "left", "right", "stop"}
        self.speeds = {"L0", "L1", "L2", "L3"}
        self.max_duration = 30_000  # ms
        self.max_commands = 30


class CommandParser:
    """命令解析器"""
    
    def __init__(self, config: LLMConfig = None):
        self.config = config or LLMConfig()
        self._validate_token()
        self.client = OpenAI(base_url=self.config.endpoint, api_key=self.config.api_key)
        
        # 构建 JSON Schema
        self.schema = self._build_schema()
        self.system_prompt = self._build_system_prompt()
    
    def _validate_token(self):
        """验证 API Token"""
        if not self.config.api_key:
            raise ValueError(f"❌ Environment variable GITHUB_TOKEN is missing.")
    
    def _build_schema(self) -> Dict[str, Any]:
        """构建 Structured Outputs Schema"""
        return {
            "name": "CarCommands",
            "strict": True,
            "schema": {
                "type": "object",
                "properties": {
                    "commands": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": self.config.max_commands,
                        "items": {
                            "type": "object",
                            "properties": {
                                "action": {
                                    "type": "string",
                                    "enum": ["forward", "backward", "left", "right", "stop"]
                                },
                                "duration_ms": {
                                    "type": ["integer", "null"],
                                    "minimum": 0,
                                    "maximum": self.config.max_duration
                                },
                                "angle_deg": {
                                    "type": ["integer", "null"],
                                    "minimum": 1,
                                    "maximum": 360
                                },
                                "speed": {
                                    "type": ["string", "null"]
                                }
                            },
                            "required": ["action", "duration_ms", "angle_deg", "speed"],
                            "additionalProperties": False
                        }
                    }
                },
                "required": ["commands"],
                "additionalProperties": False
            }
        }
    
    def _build_system_prompt(self) -> str:
        """构建系统提示词"""
        return (
            "You are a command parser. Extract an ORDERED list of car-driving commands from the user's utterance.\n"
            "Return ONLY a compact JSON object with a single key 'commands', which is an array of objects.\n"
            "Each object must have ALL of these keys: action, duration_ms, angle_deg, speed.\n"
            "Rules per item:\n"
            "- action ∈ ['forward','backward','left','right','stop'].\n"
            "- If action is 'left' or 'right': angle_deg is an integer 1..360, duration_ms is null.\n"
            "- If action is 'forward' or 'backward': duration_ms is an integer 0..30000, angle_deg is null.\n"
            "- If action is 'stop': both duration_ms and angle_deg are null.\n"
            "- speed is optional: set to 'L0'/'L1'/'L2'/'L3' if specified, otherwise null.\n"
            "Do not include any other keys. Preserve the order the user spoke."
        )
    
    def parse(self, text: str) -> List[Dict[str, Any]]:
        """
        解析文本为命令列表
        
        Args:
            text: 用户的语音识别文本
            
        Returns:
            命令列表，每个命令是一个字典
            
        Raises:
            Exception: 解析失败或验证失败
        """
        # 调用大模型
        try:
            resp = self.client.chat.completions.create(
                model=self.config.model_name,
                messages=[
                    {"role": "system", "content": self.system_prompt},
                    {"role": "user", "content": text}
                ],
                temperature=0,
                response_format={"type": "json_schema", "json_schema": self.schema},
            )
        except Exception as e:
            raise Exception(f"❌ Model call failed: {e}")
        
        # 解析响应
        content = resp.choices[0].message.content
        if not content:
            raise Exception("❌ Empty model response.")
        
        try:
            data = json.loads(content)
        except Exception:
            raise Exception(f"❌ Non-JSON model response: {content}")
        
        # 验证和标准化
        validated_data = self._validate(data)
        return validated_data["commands"]
    
    def _validate(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """验证返回的数据格式"""
        # 顶层校验
        if list(data.keys()) != ["commands"]:
            raise Exception(
                f"❌ Top-level must contain only 'commands'. Got keys: {sorted(list(data.keys()))}"
            )
        
        cmds = data["commands"]
        if not isinstance(cmds, list) or not (1 <= len(cmds) <= self.config.max_commands):
            raise Exception(
                f"❌ 'commands' must be an array with 1..{self.config.max_commands} items."
            )
        
        # 逐条校验
        for idx, item in enumerate(cmds):
            if not isinstance(item, dict):
                raise Exception(f"❌ commands[{idx}] must be an object.")
            
            expected = {"action", "duration_ms", "angle_deg", "speed"}
            if set(item.keys()) != expected:
                missing = expected - set(item.keys())
                extra = set(item.keys()) - expected
                if missing:
                    raise Exception(
                        f"❌ commands[{idx}] missing keys: {sorted(list(missing))}"
                    )
                if extra:
                    raise Exception(
                        f"❌ commands[{idx}] extra keys: {sorted(list(extra))}"
                    )
            
            action = item["action"]
            if not isinstance(action, str) or action.lower() not in self.config.actions:
                raise Exception(f"❌ commands[{idx}] invalid action: {action}")
            action = action.lower()
            
            duration_ms = item["duration_ms"]
            angle_deg = item["angle_deg"]
            speed = item["speed"]
            
            # 校验 action 与参数的对应关系
            if action in {"left", "right"}:
                if angle_deg is None:
                    raise Exception(f"❌ commands[{idx}] angle_deg must be integer for turn.")
                if not isinstance(angle_deg, int) or not (1 <= angle_deg <= 360):
                    raise Exception(f"❌ commands[{idx}] invalid angle_deg: {angle_deg}")
                if duration_ms is not None:
                    raise Exception(f"❌ commands[{idx}] duration_ms must be null for turn.")
            
            elif action in {"forward", "backward"}:
                if duration_ms is None:
                    raise Exception(f"❌ commands[{idx}] duration_ms must be integer for straight move.")
                if not isinstance(duration_ms, int) or not (0 <= duration_ms <= self.config.max_duration):
                    raise Exception(f"❌ commands[{idx}] invalid duration_ms: {duration_ms}")
                if angle_deg is not None:
                    raise Exception(f"❌ commands[{idx}] angle_deg must be null for straight move.")
            
            else:  # stop
                if duration_ms is not None or angle_deg is not None:
                    raise Exception(
                        f"❌ commands[{idx}] stop must have duration_ms=null and angle_deg=null."
                    )
            
            # 校验 speed
            if speed is not None:
                if not isinstance(speed, str) or speed not in self.config.speeds:
                    raise Exception(f"❌ commands[{idx}] invalid speed: {speed}")
            
            # 标准化 action
            item["action"] = action
        
        return data
