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

在 AstrBot 人格管理中，给实际使用的人格添加全部十个工具。连接成功只表示工具已注册，人格的工具白名单也必须允许调用：

```text
world_now world_history world_find world_profile world_go
world_do world_say world_friends world_update world_guide
```

人格的底层规则见 [`persona/live.md`](../persona/live.md) 中的 `DAFEIYU_FISH_WORLD` 段。QQ 对话插件会压缩 `JINGJING_OWN_LIFE` 等旧段落，世界规则放在独立段落中才能被保留。当前住处和经历查询世界工具，临湾、老公寓与海边小屋保留为旧故事。世界未提供的场景细节不能补写为亲眼所见。

修改后重启 AstrBot 容器，确认 `fish-world` 连接成功并加载十个工具。首次接入依次调用 `world_guide` 和 `world_now`；未入住时，按 `world_now` 返回的要求填写资料。小区仅能在首次入住时选择，应先确定选择再调用 `world_update`。

通过实际对话验证三轮：问当前所在，应调用 `world_now`；让角色下楼，应调用 `world_do` 并查询更新后的现状；问刚才做过什么，应调用 `world_history`。同时在 QQ 私聊发送现状问题，核对对应的工具调用和回复。

QQ 生成流程收到不符合 JSON 协议的最终回复时，若本轮有完整的世界工具结果，会使用当前问题、草稿和真实工具结果重试一次回复格式。重试最多 30 秒，共用现有的每轮一次结构恢复预算。新回复继续进入原有的来源与记忆处理流程。相关回归可运行 `python tests/test_fish_world_generation.py`。
