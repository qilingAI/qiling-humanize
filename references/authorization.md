# 一键授权协议与发布状态

## 当前状态

平台已实现浏览器确认与设备轮询协议，Skill 已用 Python 标准库按协议接入。**平台新增路径尚未部署，Skill 的默认入口保持关闭，等待部署与线上联调。** 不再等待回传协议设计，也不宣称现在线上可用。

默认 Key 的创建、获取和复用已有实现。新用户自动创建默认 Key，已有用户复用其默认 Key，保留用户设定的权限。被停用、删除、确认后被重置的 Key 不静默替换；用户在平台处理后重新授权。

## 目标体验

`python3 scripts/humanize.py authorize` 检查本地配置：已有 Key 时复用；未配置时，创建授权请求并打开平台返回的确认页。用户登录或注册后明确确认，Skill 通过轮询领取 Key，安全保存到本地。无自动浏览器的环境用 `authorize --no-browser` 显示确认链接，仍由同一进程等待结果。

不用 `redirect_uri`，不用 localhost 回调服务器，不读取浏览器 Cookie 或网页登录 Token，不自动替用户确认授权。

## HTTP 契约

基础地址固定为 `https://toolsoul.daniu7.cn`。下面两个接口无需 API Key，不带 `x-api-key`。

### 创建授权请求

`POST /api/v1/auth/api-key/requests`

```json
{"client_name":"文章去 AI 味 Skill"}
```

Skills 不填 `app_key`；软件才填写平台登记的 App Key。响应字段：

```json
{
  "device_code":"仅由当前进程持有的设备凭据",
  "verification_url":"平台返回的浏览器确认地址",
  "expires_in":600,
  "interval":3
}
```

只打开返回的 `verification_url`，并校验它属于器灵正式 HTTPS 同站 `/account/api-authorize` 确认页。不要自行拼 URL。浏览器确认标识与 `device_code` 不同，不能互换；后者只在本次进程内存中保留，不进入 URL、回复、日志或文件。

### 轮询领取

按响应间隔轮询，至少每 3 秒一次：`POST /api/v1/auth/api-key/token`。

JSON 只有 `device_code` 字段，值取自创建响应。`pending` 继续等待；`denied` 立即停止；`consumed` 表示已领取，不再尝试领取旧请求；HTTP 410 表示过期，HTTP 429 遵守 `Retry-After` 并降低频率。网络断开或领取响应丢失时停止，下次重新发起授权，不盲目重领。

`authorized` 只成功领取一次，包含 `api_key`、`app_key`、`base_url`、`models_url`、`model_docs_url`。本 Skill 校验 `base_url` 等于正式基础地址，`app_key` 为空，Key 格式正确，才调用 `save_key()` 保存。只输出配置成功，不输出密钥或完整授权响应。此 Skill 使用已发布的去 AI 接口，不需要访问模型文档或额外配置软件模型白名单。

## 凭据与改文

本地存储为 `~/.config/qiling-humanize/credentials.json`，POSIX 文件权限为 600。`QILING_API_KEY` 环境变量优先。手动 `configure` 仍为兼容备用，密钥只在用户自己的交互终端输入。

获得 Key 后继续 `POST /api/v1/open/remove-ai`，正文使用 `text`，请求头使用 `x-api-key`；查询改文为 `GET /api/v1/open/tasks/{task_id}`。授权 `device_code` 绝不当作改文 `task_id`。

平台新增的 `/api/v1/open/balance`、`/logs`、`/usage` 与 `/model-docs` 同样处于待部署状态。当前 Skill 不依赖这些路径，不从余额推算免费次数，也不要求安装尚未单独发布的新 SDK。

## 上线启用条件

平台完成部署后，用正式授权页受控核对：请求创建、用户明确确认、领取与私有保存、默认 Key 复用、拒绝与过期、一次领取、实际去 AI 权限。线上验证完成后将 `scripts/humanize.py` 的 `AUTHORIZATION_ENABLED` 改为 `True` 并更新发布状态说明；没有验证前保留关闭。
