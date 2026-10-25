# 关键词唤醒

`keyword` 使用已训练的 FSMN V2 模型和本地 Silero VAD，不启动语音会话。使用独立的 `keyword` extra 安装依赖，无需安装包含 Linux 音频处理的整套 `voice` extra。模型放在实例的 `models/fsmn-v2/`，包含 `manifest.json`、`model.onnx`、`cmvn.npy`、`tokens.txt`、`lexicon.json`；VAD 使用 `models/silero_vad.onnx`。加载校验清单、校验和和已训练的唤醒短语，不自动替换为其他模型。

Windows Server 已验证 ONNX Runtime CPU 与 Kaldi 原生库加载、特征计算和缺失模型的明确失败。当前没有既有的训练模型包，实际关键词推理未验收。插件已声明 Linux／Windows 代码支持；启用时仍要求有效训练模型包，不自动填充或绕过校验。不能用缺失模型报错或合成推理冒充唤醒成功。语音入口继续停用。

已有模型包可用 `scripts/download_models.py --wake-bundle <模型包目录> --model-dir <独立验收实例模型目录>` 准备；脚本验证既有模型，不训练或下载替代唤醒模型。
