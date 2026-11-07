# 阿里百炼语音适配

可选扩展，仅提供语音流，不持有宿主，不进行 Agent 推理。
需要 ai_usage 与私有环境凭据 `DASHSCOPE_API_KEY`。

- 地域：北京；默认 WebSocket 地址为 `wss://dashscope.aliyuncs.com/api-ws/v1/inference`。
- ASR：`fun-asr-realtime`，16 kHz PCM16；显式 finish-task 后收集最终句段。
- TTS：`cosyvoice-v3-flash`，24 kHz PCM16；默认中文音色 longanyang，语速 1.0。
- `capabilities` 可单独启用识别或合成，以便另一能力使用别的适配；同类能力不能重复登记。
- 凭据从启动快照读取，网页只展示是否配置。没有凭据时降级，不付费探测健康。
- 不启用自动重试或供应商回退；流中断不会重做用户业务。

费用通过统一 ai_usage 准入：识别预留最大秒数、合成预留实际字符数；完成后
按发送的音频秒数／文本字符数结算。无法确认费用时保留预留。价格使用
`audio_cny_per_second`、`text_cny_per_character`；本地计算是估算，不是供应商账单。
修改价格、模型和用量规则仍走统一配置应用流程。

协议依据：[Fun-ASR](https://help.aliyun.com/zh/model-studio/fun-asr-realtime)、
[语音合成](https://help.aliyun.com/zh/model-studio/tts-model)。
`AllocationQuota.FreeTierOnly` 表示免费额度策略拒绝，请在百炼控制台处理，不能靠原样重试解决。

真实合成和识别均已用合成内容在北京接口跑通；更多音色、真人噪声场景和 PP
扬声器音质不由该测试证明。详见 ai_passport/docs/acceptance.md。
