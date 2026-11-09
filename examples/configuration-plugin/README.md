# 独立插件页面模板／验收插件

这是一个可安装 Python 插件，含完整 React/TypeScript/Vite 页面。
只使用公共 API 和 SDK，不导入宿主或其它插件实现，不访问真实设备或模型。

## 独立预览

从仓库根目录执行：

```sh
npm ci --ignore-scripts --prefix examples/configuration-plugin/ui
npm run dev --prefix examples/configuration-plugin/ui
```

Vite 页面单独打开时使用 mockClient，数据仅在内存中，**不会保存 PA 配置**。
嵌入后台时自动使用真实 SDK。修改 SDK 源码后重新 `npm ci` 更新本地文件依赖。

## 构建、安装、启用

```sh
npm run build --prefix examples/configuration-plugin/ui
uv build --wheel examples/configuration-plugin
# 在目标实例所用的 Python 环境安装刚构建的 wheel：
uv pip install --python /绝对路径/python examples/configuration-plugin/dist/personal_agent_configuration_demo-1.0.0-py3-none-any.whl
pa plugin-ui-check
```

在实例 plugins 列表加入 `configuration_demo`，不替换已有列表。自身配置：

```toml
[plugin_settings.configuration_demo]
label = "我的插件"
mode = "local"
```

重启管理服务以发现新安装的包。插件业务未启用或停止时，页面仍可打开编辑配置；
只有查询/登记等运行操作要求插件已启用且就绪。添加插件属于组成变化，需要确认重启。

页面同时演示：自动控件、自定义动态对象、条件字段、私有口令、文本/JSON/凭据资源，
以及两步登记向导。所有配置使用外层保存栏，只有业务操作有自己的按钮。

## 复制为新插件

修改 Python 包名、发行名、入口点、plugin.json 的 id/版本、configuration.json 的
namespace 和页面标题，保留相互一致。新增字段/动作由插件维护，中央前端无需修改。
开发到交付保持 `management/` 构建产物随 wheel 打包。

SDK 尚未发布到公共 npm。脱离本仓库开发时先：

```sh
npm pack ./packages/plugin-ui --pack-destination /你的依赖目录
```

在新项目用生成的 `personal-agent-plugin-ui-1.0.0.tgz` 作为 file 依赖，并提交自己的
package-lock.json。生产构建不需要源码仓库中 packages 目录的存在。

## 隔离验收

不要把测试插件安装进正在运行的生产环境。可安装到临时 target，并在子进程 PYTHONPATH 中引用：

```sh
uv pip install --python .venv/bin/python --no-deps --target /tmp/pa-page-installed examples/configuration-plugin/dist/personal_agent_configuration_demo-1.0.0-py3-none-any.whl
PYTHONPATH=src:/tmp/pa-page-installed .venv/bin/python scripts/verify_plugin_pages.py
```

脚本需要 Playwright/Chromium，创建临时实例，启动真实管理和业务进程；不连接外部模型、
设备或生产实例。也可将框架 wheel 安装至独立 target，以该目录替代 src 验收发布包。

详细契约见 [插件页面规范](../../docs/development/plugin-management-pages.md)。
