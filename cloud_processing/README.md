# Realtime Speech → Structured Driving Command

将语音或文字实时解析为严格结构化的小车驾驶指令。语音在本地用 faster-whisper 转写，转写出的文本通过 GitHub Models（OpenAI 格式 API）解析成 JSON 指令；本地进行强校验并只输出合规 JSON，不做兜底或推断。

## 功能概览
- 输入：
  - 文字（直接传入）
  - 音频文件（.m4a 或 .wav；自动将 .m4a 转为 16kHz 单声道 WAV 再转写）
- 语音转文字：faster-whisper（本地运行）
- NLU 解析：GitHub Models（OpenAI Chat Completions API 兼容）
- 输出：严格、最小化的 JSON 指令，仅包含允许字段
- 本地强校验：动作与字段的必填/禁填、类型/范围检查，出错直接退出

## 目录结构
- `realtime_text.py` 主程序

## 运行环境与依赖
- 系统：macOS（其他平台理论可行，命令略有差异）
- Python：3.9+（推荐 3.10/3.11）
- ffmpeg：用于音频转码（必需）
- Python 包：
  - `faster-whisper`
  - `openai`
  - `numpy`

### 安装步骤（推荐在虚拟环境中）
```zsh
# 1) 建立并启用虚拟环境
python3 -m venv .venv
source .venv/bin/activate

# 2) 安装依赖（可使用 requirements.txt）
pip install --upgrade pip
pip install -r requirements.txt

# 3) 安装 ffmpeg（若尚未安装）
brew install ffmpeg
```

如果不使用 requirements.txt，也可以直接安装：
```zsh
pip install faster-whisper openai numpy
```

## 配置
脚本中默认的关键常量（可在 `.env` 或 `realtime_text.py` 顶部修改）：
- ENDPOINT / OPENAI_BASE_URL: 默认 `https://models.github.ai/inference`
- MODEL_NAME / OPENAI_MODEL: 默认 `openai/gpt-4o`
- WHISPER_MODEL: 默认 `base`（可选 tiny/base/small/medium）
- WHISPER_DEV: 默认 `cpu`
- WHISPER_CT: 默认 `int8`
- LANG_HINT: 默认 `None`（设为 "zh" 可固定中文）
- ACTIONS: `{forward, backward, left, right, stop}`
- SPEEDS: `{L0, L1, L2, L3}`
- MAX_DURATION: 默认 `30000` 毫秒

### 身份凭证（强烈建议改为环境变量）
当前文件中的 `ensure_token()` 写死了一个 token，仅用于演示。为安全起见，建议：
- 将 `ensure_token()` 改为从环境变量读取（例如 `GITHUB_TOKEN`）
- 永远不要把真实 token 提交到仓库

建议替换实现（示例）：
```python
import os
import sys

def ensure_token() -> str:
    tok = os.getenv("GITHUB_TOKEN", "")
    if not tok:
        print("❌ Env GITHUB_TOKEN missing.")
        sys.exit(2)
    return tok
```

设置环境变量或使用 `.env` 文件（推荐）：
```zsh
export GITHUB_TOKEN="<你的 GitHub Models 访问令牌>"
# 或在 cloud_processing/.env 中填写：
# GITHUB_TOKEN=<你的令牌>
# OPENAI_BASE_URL=https://models.github.ai/inference
# OPENAI_MODEL=openai/gpt-4o
# WHISPER_MODEL=base
# WHISPER_DEV=cpu
# WHISPER_CT=int8
# LANG_HINT=zh
```

> 提示：本项目仅将“转写后的文本”发送到模型；音频转写在本地完成。

## 使用方法
进入项目目录后，确保虚拟环境已激活并已配置好 token（`.env` 或环境变量）。

### 1) 直接解析文字
```zsh
python realtime_text.py --text "向前走三秒，速度L2"
```

### 2) 解析音频文件（.m4a 或 .wav）
```zsh
# .m4a 会自动转为 16k 单声道 WAV 再转写
python realtime_text.py sample.m4a
```

程序会在控制台输出严格 JSON（无多余说明），例如：
```json
{"action":"forward","duration_ms":3000,"speed":"L2"}
```

控制台还会打印一行 STT 文本预览（音频模式），形如：
```
👤 STT: 向前走三秒，速度L2
```

当输入无效或模型输出不合规时，程序会打印错误并以非零状态码退出。

## 输出 JSON 规范
- 通用字段：
  - action: 必填，枚举值之一：`forward | backward | left | right | stop`
  - speed: 可选，枚举值之一：`L0 | L1 | L2 | L3`
- 条件字段：
  - 对于 `forward/backward`：
    - 必须包含 `duration_ms`（整数，范围 0..30000）
    - 禁止出现 `angle_deg`
  - 对于 `left/right`：
    - 必须包含 `angle_deg`（整数，范围 1..360）
    - 禁止出现 `duration_ms`
  - 对于 `stop`：
    - 不得包含 `duration_ms` 或 `angle_deg`

> 解析逻辑以 `SYSTEM_PROMPT` 指令为准，响应格式通过 `response_format={"type":"json_object"}` 强制模型返回 JSON；随后在本地进行严格字段和范围校验。

## 常见问题与排错
- ffmpeg 未安装或未在 PATH：
  - 现象：提示 “ffmpeg not found. Install it first.”
  - 解决：`brew install ffmpeg`
- faster-whisper 安装失败或慢：
  - 可升级 pip：`pip install --upgrade pip`
  - M 系列 Mac 也可直接用 CPU；如需更快速度，可将 `WHISPER_MODEL` 设为 `tiny`/`base`，或研究 GPU/Metal 支持（本脚本默认 `cpu`）
- 模型调用报错（如 401/403/404/网络问题）：
  - 检查 `GITHUB_TOKEN` 是否有效，并确认 `ENDPOINT` 与 `MODEL_NAME` 可用
  - 网络环境需能访问 GitHub Models 推理端点
- 返回 “Non-JSON model response” 或 “Extra unexpected keys”：
  - 多因模型未按 JSON 输出或包含了多余字段；程序不会兜底，请重试或调整输入表达
- STT 结果为空：
  - 检查音频是否有有效语音、采样率/声道是否正确；或尝试更换 `WHISPER_MODEL`

## 扩展与定制
- 修改语言提示：将 `LANG_HINT = "zh"` 可固定中文，减少自动检测的不确定性
- 修改动作/速度集合与上限：调整 `ACTIONS`、`SPEEDS`、`MAX_DURATION`
- Whisper 模型与精度/速度：
  - `WHISPER_MODEL`：`tiny` < `base` < `small` < `medium`（体积/耗时/精度递增）
  - `WHISPER_CT`：当前为 `int8`，更高精度可考虑 `int16`/`float16`（更慢更耗内存）

## 安全与隐私
- 音频仅在本地转写，不会上传。
- 仅将转写后的文字与系统提示发送到大模型端点。
- 请务必使用环境变量方式管理访问令牌，并避免提交到代码仓库。

## 许可
用于课程/项目示例，可根据需要自行添加 LICENSE 文件与声明。

## 快速命令速查
```zsh
# 激活环境
source .venv/bin/activate

# 文字解析
python realtime_text.py --text "左转90度"

# 音频解析
python realtime_text.py voice.m4a
```

### macOS 实时多命令模式（`mac_realtime_multi.py`）

#### 查看可用麦克风设备
```zsh
# 1) 先看看你的麦克风索引
python -c "import sounddevice as sd; print(sd.query_devices())"
```

#### 运行实时语音识别
```zsh
# 2) 运行（可选指定设备索引）
python mac_realtime_multi.py --device 2 --print-stt
# 系统会第一次提示给终端麦克风权限：在 系统设置 → 隐私与安全性 → 麦克风 勾上你的终端

# 不指定设备索引时，将自动选择可用的输入设备
python mac_realtime_multi.py --print-stt
```

#### 支持的环境变量配置（`.env` 文件）
- GITHUB_TOKEN, OPENAI_BASE_URL, OPENAI_MODEL
- WHISPER_MODEL, WHISPER_DEV, WHISPER_CT, LANG_HINT
- SAMPLE_RATE, CHANNELS, FRAME_MS, MAX_SEG_SEC
- RMS_WIN_MS, RMS_THRESH_DB, RMS_HANG_MS_ON, RMS_HANG_MS_OFF, RMS_FLOOR_DECAY
- MAX_DURATION, MAX_COMMANDS, PRINT_STT_DEFAULT
