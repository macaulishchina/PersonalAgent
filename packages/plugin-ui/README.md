# @personal-agent/plugin-ui

插件管理页 SDK v1，TypeScript 源码由插件自身的 Vite 构建编译。
不依赖 PA 中央前端，不共享 React 实例，不包含管理员凭据或保存入口。

- `connect()`：iframe 中连接外层草稿与管理操作。
- `mockClient()`：独立开发时的内存模拟。
- `react`：可选状态 hook 与标量自动表单。
- `theme.css`：可选 CSS 模板。

参见 [公开规范](../../docs/development/plugin-management-pages.md) 及
[完整开发模板](../../examples/configuration-plugin/README.md)。
