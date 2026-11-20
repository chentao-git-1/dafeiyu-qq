# QQ 语音识别

听澜通过阿里云百炼北京工作空间的 `qwen3-asr-flash` 识别 QQ 语音。AstrBot 将 QQ 的 SILK 等语音格式转为 WAV，把音频提交给百炼，转写文字进入当前会话。群聊沿用已有的真实 @ 和白名单规则。

主人直接发来的语音还可以带有一个粗略的语气标签，例如愉快、平静或难过。程序只把有效标签附在本轮聊天模型请求里，听澜结合说话内容决定怎样回应。引用语音的文字仍留在引用消息中。缺少标签或同一录音的分段判断不一致时，照常使用转写文字。

## 配置

1. 启用 `dafeiyu_runtime` 和 `dafeiyu_bailian_tts` 插件。
2. 在 AstrBot 新建类型为 `dafeiyu_bailian_asr` 的语音识别提供商，ID 可设为 `dafeiyu-bailian-asr`，模型填 `qwen3-asr-flash`。升级已有提供商时，修改这一项并保存即可。原 `qwen-audio-3.1-asr-flash` 配置仍支持纯文字转写。
3. 已配置百炼语音合成时，将 `credential_provider_id` 填为该提供商的 ID，例如 `dafeiyu-voice`，即可共用它的北京工作空间和 API Key。如果单独配置识别凭据，将该字段留空，并在私有配置中设置 `workspace_id` 和 `api_key`。
4. 在 QQ 使用的配置文件中启用语音转文字，并选择刚创建的 STT 提供商。私聊可直接发送语音。

普通短语音在一次请求中取得文字和语气。超过五分钟或编码大小上限的录音会按顺序分段。发往识别接口的内容只有待识别的音频。接口规格、语气标签和北京区计费见[阿里云 Qwen-ASR 文档](https://help.aliyun.com/zh/model-studio/qwen-asr-api-reference)和[百炼价格表](https://help.aliyun.com/zh/model-studio/model-pricing)。

安装框架依赖后运行 `python scripts/run_tests.py test_qq_voice_input.py`。仓库测试使用模拟响应，不会调用收费接口。
