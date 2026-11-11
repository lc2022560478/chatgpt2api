# RT 刷新 AT

## 页面操作

- 直接导入 RT：进入「号池管理」→「导入」→「导入 Refresh Token（RT 换 AT）」，每行一个 RT，单次最多 100 个。
- 给已有账号绑定 RT：点击账号行的编辑按钮，在「Refresh Token（RT）」中填写并保存。留空并保存可移除已绑定的 RT。
- 单账号续期：点击账号行的钥匙图标「RT 刷新 AT」。未绑定 RT 的账号不可使用此操作。
- 批量续期：使用「RT 刷新全部 AT」，或勾选账号后使用「RT 刷新选中 AT」。没有 RT 的账号会计入跳过数量。
- RT 续期与「刷新账号信息和额度」是独立操作；已有的到期前自动续期和 RT 定期保活继续使用同一条刷新链路。

成功后会保存新 AT；上游返回新 RT 或 ID Token 时会同步保存，没有返回时保留原值。账号代理、邮箱、套餐、额度、使用统计等既有字段不会因为凭据替换而重置。

直接导入 RT 时会在保存凭据后尝试同步账号信息。信息同步失败只作为警告，不会把已成功换取的凭据删除。手动 RT 续期失败也不会删除账号或自动触发密码重新登录。

## 到期时间

- 账号列表和编辑窗口展示「AT 到期时间」，使用北京时间，并显示剩余时间、临近到期或已到期状态。按界面精简要求，不展示 RT 到期时间；后台仍保留对应字段及同步逻辑。
- 优先从对应 Token 的 JWT `exp` 读取期限；没有 JWT 到期信息时读取上游或导入文件的明确到期字段。AT 支持 `access_token_expires_at`、`expires_at`、`expired`、`expires`；RT 支持 `refresh_token_expires_at`、`refresh_expires_at`。
- 上游返回 `expires_in` / `access_token_expires_in` 或 `refresh_token_expires_in` / `refresh_expires_in` 时，在接收凭据时一次性换算成绝对时间。账号读取、容器重启不会重新计时。
- 手动续期、自动续期、RT 导入、OAuth 登录和密码重新登录都会同步对应凭据的到期时间。Token 轮换但上游没有提供新期限时清除旧日期；Token 未变化且没有新期限时保留已有日期。刷新失败保留原凭据和到期时间。
- 未绑定 RT 的账号不可使用 RT 续期操作。不透明 RT 且上游未返回有效期时，后台 RT 到期字段为 `null`。参考项目没有给出固定 RT 到期日期，本项目也不会以 AT 到期日期或保活周期推算 RT 到期时间。
- 管理接口返回 `access_token_expires_at`、`refresh_token_expires_at`，格式为 UTC ISO 8601，未知时为 `null`。JSON 导出包含已知的到期时间，重新导入和容器重建后保持一致。前台每 30 秒更新倒计时，并在空闲且页面可见时同步后台自动续期结果。

账号表格将邮箱、AT 和创建时间合并为账号列，将类型与来源、在途/成功/失败统计分别合并展示。窄屏使用纵向账号行，保留日期、额度、统计和全部操作按钮，避免依靠横向滚动查看操作列。

## 上游请求

请求参数按提供的参考图实现，不额外进行授权登录或密码登录：

```http
POST https://auth.openai.com/oauth/token
Content-Type: application/json
```

```json
{
  "client_id": "app_2SKx67EdpoN0G6j64rFvigXD",
  "grant_type": "refresh_token",
  "redirect_uri": "com.openai.chat://auth0.openai.com/ios/com.openai.chat/callback",
  "refresh_token": "rt_example"
}
```

上游请求沿用项目现有代理选择顺序，优先使用账号代理，其次使用上游代理运行时。错误中保留 HTTP 状态与错误类型，过滤 Token 和非 JSON 响应正文，避免凭据进入错误日志。

## 管理接口

以下接口要求管理员密钥，不能使用普通用户密钥。

强制刷新账号 AT；`access_tokens` 省略或为空时表示全部账号：

```bash
curl -X POST http://localhost:3000/api/accounts/refresh-token \
  -H "Authorization: Bearer $ADMIN_KEY" \
  -H "Content-Type: application/json" \
  -d '{"access_tokens":["existing_access_token"]}'
```

直接换取并导入 RT：

```bash
curl -X POST http://localhost:3000/api/accounts/refresh-token/import \
  -H "Authorization: Bearer $ADMIN_KEY" \
  -H "Content-Type: application/json" \
  -d '{"refresh_tokens":["rt_example"]}'
```

两个接口都立即返回 `{"progress_id":"..."}`。使用 `GET /api/accounts/refresh/progress/{progress_id}` 查询进度，`done=true` 时读取 `result` 中的 `refreshed`、`skipped`、`added`（导入接口）、`errors` 和 `warnings`（导入接口）。账号列表仅通过管理员鉴权的接口返回；不要将返回结果公开。

给已有账号保存 RT：

```bash
curl -X POST http://localhost:3000/api/accounts/update \
  -H "Authorization: Bearer $ADMIN_KEY" \
  -H "Content-Type: application/json" \
  -d '{"access_token":"existing_access_token","refresh_token":"rt_example"}'
```

## Docker 更新

前端和后端都包含在应用镜像中，修改源码后需要重建镜像并重建应用容器：

```bash
docker compose build app
docker compose up -d --no-deps app
```

当前 Compose 将 `./data` 挂载到 `/app/data`，将 `./config.json` 挂载到 `/app/config.json`。JSON 存储下的新 AT、RT 会写入持久化的账号文件，容器重建后继续保留。其他存储后端仍通过各自的 `StorageBackend` 保存。

页面未出现新入口时刷新浏览器以加载新静态资源。无需删除数据目录，也不需要重启 WARP、Privoxy 或 FlareSolverr。

RT 属于敏感凭据，应仅使用有权管理的账号。换取结果依赖 RT 的有效性及其所属 OAuth 客户端；上游返回 `invalid_grant`、`app_session_terminated` 等错误时，需要按账号实际授权状态重新获取凭据。
