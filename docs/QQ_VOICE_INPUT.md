# QQ 语音识别

听澜通过阿里云百炼北京工作空间的 `qwen-audio-3.1-asr-flash` 识别 QQ 语音。AstrBot 将 QQ 的 SILK 或其他语音格式转为 WAV，把音频直接提交给百炼，返回的文字进入当前 QQ 会话。群聊沿用已有的真实 @ 和白名单规则。引用消息中的语音保留原说话人的归属。

## 配置

1. 启用 `dafeiyu_runtime` 和 `dafeiyu_bailian_tts` 插件。
2. 在 AstrBot 新建类型为 `dafeiyu_bailian_asr` 的语音识别提供商，ID 可设为 `dafeiyu-bailian-asr`，模型填 `qwen-audio-3.1-asr-flash`。
3. 已配置百炼语音合成时，将 `credential_provider_id` 填为该提供商的 ID，例如 `dafeiyu-voice`，即可共用它的北京工作空间和 API Key。如果单独配置识别凭据，将该字段留空，并在私有配置中设置 `workspace_id` 和 `api_key`。
4. 在 QQ 使用的配置文件中启用语音转文字，并选择刚创建的 STT 提供商。私聊可直接发送语音。

通常一条语音对应一次识别请求。超过接口单次时长上限的录音会按顺序分段。请求中只携带需要识别的音频。接口规格、输入格式和响应字段见[阿里云百炼非实时语音识别 API](https://help.aliyun.com/zh/model-studio/qwen-audio-asr-http-api)。

安装框架依赖后运行 `python scripts/run_tests.py test_qq_voice_input.py`。仓库测试使用模拟响应，不会调用收费接口。
