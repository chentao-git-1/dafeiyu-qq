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

密钥由主人在鱼的世界网站签发，只写入服务器的 MCP 配置文件。不要提交配置文件、密钥、聊天数据或世界状态到 GitHub。修改配置后重启 AstrBot 容器，确认 `fish-world` 连接成功并加载十个工具。

人格的底层规则见 [`persona/live.md`](../persona/live.md)。首次接入依次调用 `world_guide` 和 `world_now`；未入住时，按 `world_now` 返回的要求填写资料。

