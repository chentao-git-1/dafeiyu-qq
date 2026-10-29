# 鲸鲸聊天语音

保留原百炼提供商，同时注册 `dafeiyu_breeze_tts` 原生 TTS 适配器。Breeze 使用账户中已经保存的音色，不重新设计或克隆，不安装额外运行库，不依赖本机电脑开机。

参考 [BreezeBlue 官方流式接口](https://docs.breezeblue.ai/api-reference/text-to-speech/stream-text-to-speech)，配置 `model: breeze-tts-2`、本人账户的 `voice`、`api_key`，默认 `guidance_scale: 2`。真实密钥和音色 ID 只写入仓库外的 AstrBot 私有配置。仅向固定官方 HTTPS 地址发送鉴权，不跟随重定向。

复用现有关系及语气上下文，表演提示由固定规则生成，用户原文不拼接为表演指令。保留语音总开关、概率、会话偏好、单段合成和失败回退文字；请求发出后不重试、不切换到另一个音色、不执行充值或订阅操作。

服务器使用 IPv4，TCP/TLS 连接失败时最多重连两次，每次连接上限五秒。Breeze 使用 85 秒总时限与 75 秒读取时限，语音发送层最多等待 90 秒；其他提供商仍使用原等待时间。只重试尚未发送请求的连接错误，不重试 HTTP 响应错误、读取错误或已发出的合成请求，避免重复扣点；参见 [HTTPX 官方传输文档](https://www.python-httpx.org/advanced/transports/)。

对流式 WAV 修复文件头，并检查 24kHz、16bit、单声道、大小、实际时长及非静音。输出最多 12 MiB，只清理本适配器自己的过期缓存。错误不传播响应正文、请求头或密钥。
