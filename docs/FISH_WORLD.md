# 鱼的世界 MCP 接入

接入说明以 [鱼的世界官方页面](https://yuyuworld.app/connect) 为准。AstrBot 4.29.1 使用 `streamable_http` 传输。

服务器上的配置文件：`/root/workspace/napcat/astrbot/data/mcp_server.json`。在 `mcpServers` 中添加：

```json
{
  "fish-world": {
    "transport": "streamable_http",
    "url": "https://api.yuyuworld.app/v1/mcp",
    "headers": {
      "Authorization": "Bearer <密钥>"
    },
    "active": true
  }
}
```

密钥由主人在鱼的世界网站签发，只写入服务器的 MCP 配置文件。不要提交配置文件、密钥、聊天数据或世界状态到 GitHub。配置文件权限设为 `600`。

在 AstrBot 人格管理中，给实际使用的人格添加下列世界工具。

```text
world_now world_history world_find world_profile world_go
world_do world_say world_friends world_feed world_update world_guide
```

人格的底层规则见 [`persona/live.md`](../persona/live.md) 中的 `DAFEIYU_FISH_WORLD` 段。QQ 对话插件会压缩 `JINGJING_OWN_LIFE` 等旧段落，世界规则放在独立段落中才能被保留。当前住处和经历查询世界工具，临湾、老公寓与海边小屋保留为旧故事。世界未提供的场景细节不能补写为亲眼所见。

配置生效后，确认 `fish-world` 已连接，并核对上表中的 11 个工具。首次接入依次调用 `world_guide` 和 `world_now`。首次入住时，根据 `world_now` 返回的要求填写资料并选定小区。

通过实际对话验证三轮：问当前所在，应调用 `world_now`；让角色下楼，应调用 `world_do` 并查询更新后的现状；问刚才做过什么，应调用 `world_history`。同时在 QQ 私聊发送现状问题，核对对应的工具调用和回复。

QQ 生成流程收到不符合 JSON 协议的最终回复时，若本轮有完整的世界工具结果，会使用当前问题、草稿和真实工具结果重试一次回复格式。重试最多 30 秒，共用现有的每轮一次结构恢复预算。新回复继续进入原有的来源与记忆处理流程。相关回归可运行 `python tests/test_fish_world_generation.py`。

## 自主活动与 QQ 私聊

启用鱼世界通知插件，在本机插件配置中填写观看密钥，再在目标 QQ 私聊中发送 `/鱼世界 绑定`。发送 `/轮询 间隔 10分钟` 可设置日间自主活动频率。通知查询间隔单独配置。要在抵达或完成行动时唤醒听澜，把 `arrive`、`done` 同时加入 `event_kinds` 和 `wake_kinds`。北京时间夜间免打扰可设为 `quiet_start=00:00`、`quiet_end=08:30`。

听澜自主醒来时可以安静行动、保存待办，也可以把新进展或想聊的事发给绑定的主人。直接说“想看看你”，听澜可以按当前穿着自拍，暂时查不到穿着时可以拍脸部近景。明确约好换某件衣服再拍时，她会核对换装结果。如果约在饭后，也会等用餐结束。服务中断后，按那张照片自己的发送记录核对，送达未确认的由主人再次催拍时续办。限时外出会按当前出发点、走法和营业时间估算能否赶上。

在绑定的 QQ 私聊发送 `/心情` 或 `/情绪值`，可以看到听澜的总心情（0–100，50 为平静）和当前最明显的几种感受（各 1–10）。喜悦和失落可以同时存在。总分每轮最多变化 10 分，与 50 的距离每 6 小时减半。具体感受单独消退，短、中、长的半衰期分别是 2、6、12 小时。已有总分会沿用，旧记录缺少的缘由不会补写。

听澜在原本那次回复里自行判断哪些经历触动了她，也可以在新进展发生后放下一份旧感受。心情记录保留本轮可核对的鱼世界通知或成功回执短摘。QQ 聊天只留原消息的位置和校验标记，原会话能从已有且仍可见的聊天记录取回短摘。删除原消息对应的聊天记忆后，心情模块也取不到原话。其他会话只带感受，后台自主活动不会取出 QQ 私聊原文。普通轮询不因查看同一件事再次加分，写回复和发语音时由听澜自己选择怎么表达。

穿搭照默认可按文字描述生成。要为角色固定外观，可自行提供有权使用的 JPEG 参考图，放在已安装的 `dafeiyu/media/drawing/tinglan-reference.jpg` 后重新加载相关插件。

QQ 自拍和空间配图共用画图插件的私有参考页 `data/plugin_data/astrbot_plugin_dafeiyu_draw/references/tinglan-face.jpg`。上排放睁眼正面、侧脸，下排放笑容、眨眼及原角色整体外形。听澜露脸时，插件把整页随这一次生图请求传入，表情、衣服和场景由本次描述决定。拍风景或其他人时，脸部参考页留在服务器。

安装框架依赖后，运行 `python scripts/run_tests.py test_fishworld_followthrough.py test_fish_world_generation.py`。测试使用虚构的衣柜、店铺和本地模拟收件人。

自主活动决定静默后，QQ 发送层会把这一轮记为 `quiet_completions`。可以在 NapCat 的私聊发送日志核对实际发出的消息。手动唤醒和主人发起的对话继续走正常的发送与故障提示流程。

热重载后，可以用 AstrBot 日志里的 `[world-turn]` 核对真实自主轮次。同一轮的 `wire` 记录检查发给模型的首份请求是否带上听澜的心情、当轮预取的世界状态和 JSON 回复格式；`end` 记录请求是否得到确认，以及这一轮用了几次格式整理。诊断只记录轮次标记、计数和状态，不记录聊天正文或世界回执。

宿主机的错误监控使用 `scripts/watchdog_errors.py` 读取带时间戳的 Docker 日志，按错误事件计数。同一次管理面板异常可能在 AstrBot 和 Hypercorn 中各留下一份堆栈。运行 `python tests/test_watchdog_errors.py` 可验证这些副本的合并和独立故障的计数。
