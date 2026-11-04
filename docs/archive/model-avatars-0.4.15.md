# 内置头像库交付（0.4.15）

日期：2026-11-04。包含 30 个提供商品牌图标、40 个通用简洁头像和 1 个默认助手头像，共 71 项。

品牌覆盖 OpenAI、Anthropic、Google、Meta、Mistral、xAI、Cohere、Perplexity、Hugging Face、Microsoft、NVIDIA、AWS、Groq、AI21、OpenRouter，以及 DeepSeek、通义、Kimi、智谱、MiniMax、豆包、文心、混元、阶跃、百川、讯飞、商汤、零一万物、书生、硅基流动。
通用头像包含机器人、动物、自然、几何、工具等线条图案，配有柔和底色；品牌图标有统一浅色画布，适用于深浅背景。

配置页提供图墙预览、中英文搜索、选择状态和键盘操作；支持连接头像继承、自定义上传、恢复默认及取消草稿。
资源、摘要和许可随包离线提供，运行时不访问 CDN，不调用模型。品牌 SVG 固定到 Lobe Icons 提交版本；通用图案复用已有 Lucide 图标，不新增运行依赖。

## 兼容接口

- 原有 builtin 名称和 `{kind:"builtin",name}` 目录投影保持；上传 PNG 的接口保持。
- 新增 `ModelRequest("avatars")` 读取目录，`ModelRequest("avatar_read", {"builtin":NAME,"version":VERSION})` 读取内置 SVG。
- 管理端新增受鉴权的 `/api/model-avatars`、`/api/model-avatar/builtin/NAME?version=VERSION`，支持 ETag。
- 内置资源仅按登记名读取，拒绝任意路径、未知名称和过期版本；用户上传仍拒绝 SVG。
- 头像选择不改变模型路由、预算、任务或会话行为；公共 API 2、data_format 1 不变，没有插件大版本升级。

调用示例见 [公开模型路由与头像](../guides/model-routes.md#外观与头像)。来源、固定版本、每项哈希及许可证位于 `core/models/avatars/`；维护者显式运行 `scripts/update_builtin_avatars.py` 更新。

## 验证

定向 52 项测试通过：所有资源和摘要、SVG 安全边界、许可、schema、公开目录/读取、鉴权/缓存及旧模型/头像/路由行为。
Edge 验证全部 71 张图片实际解码，以及搜索选择、继承预览、上传、保存/取消和 375/768/1280 宽度；原模型选择器回归通过。
候选 wheel 包含全部图标、目录和许可证，并在隔离安装目标验证，未替换在用安装或静态产物。原执行功能证据继续复用，不重复模型调用。
本次不部署、不重启服务。预览与私有验收产物在 `state/acceptance/avatar-library/`。
