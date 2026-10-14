# 已发布的去 AI 接口

来源：[器灵在线调用文档](https://toolsoul.daniu7.cn/account/api-docs?endpoint=remove_ai)，2026-10-14 通过已登录页面核对。此技能使用下面的已发布接口，不依赖本地开发分支里的新版 SDK 或 `/open/balance`、`/open/logs`、`/open/usage`。

服务地址：`https://toolsoul.daniu7.cn`。请求头：`x-api-key: <用户 API Key>`，JSON 请求同时带 `Content-Type: application/json`。凭据是用户 Key，不是软件授权码或网页登录 Token。

## 提交

`POST /api/v1/open/remove-ai`

```json
{"text": "需要去 AI 味的正文"}
```

`text` 必填，1–2000 字符。文档未列出风格或强度参数。当前页面价格为 0.10 元/次，不将此价格或免费次数写成固定结算规则。

```json
{
  "call_id": "示例调用ID",
  "task_id": "示例任务ID",
  "status": "queued",
  "charged": "0.100000",
  "balance": "99.90"
}
```

`charged` 是实际本次扣费，`balance` 是余额；示例没有个人剩余免费次数字段。新用户可以使用平台发放的免费次数，额度活动可能调整。免费额度和剩余次数以平台实际权益为准，不能从余额推算。

## 查询

`GET /api/v1/open/tasks/{task_id}`，使用同一个 `x-api-key`。每 3 秒查询一次，查询不收费。

```json
{
  "success": true,
  "call_id": "示例调用ID",
  "task_id": "示例任务ID",
  "status": "completed",
  "text": "处理后的文本内容"
}
```

`queued` / `processing` 继续查询；`completed` 读取 `text`；`failed` 读取 `message` 和 `error_code`，最终失败自动退费。

拿到任务 ID 后只查询原任务。提交超时且未获得 ID，先到[用量页](https://toolsoul.daniu7.cn/account/api-usage)核对，避免重复计费。

## 错误

| HTTP | 代码 | 处理 |
|---|---|---|
| 401 | `INVALID_API_KEY` | 检查 Key 是否完整、已重置或删除 |
| 403 | `API_KEY_DISABLED` / `ENDPOINT_NOT_ALLOWED` | 用户到密钥页处理启用状态或接口权限 |
| 402 | `INSUFFICIENT_BALANCE` | 提示余额不足并提供个人中心入口 |
| 404 | `TASK_NOT_FOUND` / `ENDPOINT_NOT_FOUND` | 核对任务 ID、账号归属和接口状态 |
| 422 | `INVALID_INPUT` | 检查正文、字符上限和 JSON 格式 |
| 429 | `RATE_LIMITED` | 查询遵守 `Retry-After`；提交不自动重试 |
| 503 | `UPSTREAM_NOT_CONFIGURED` / `UPSTREAM_UNAVAILABLE` | 提示服务暂不可用，保留原任务记录 |
