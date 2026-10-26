# Windows 隔离原型：2026-10-26

结论：**固定合成检查通过 12 项原生测试，证明该启动方式下的隔离原型可行。尚不是生产资源代理或任意工具链认证，不得据此启用完整权限的受控修复。** 配套[联合设计](controlled-execution-design-2026-10.md)。

## 环境与范围

Windows Server 2025 Standard，10.0.26100，64 位；Medium Mandatory Level，没有启用的 Administrators 组。本次未提升权限、安装服务、访问模型或真实凭据，未修改生产配置与目录 ACL。

只创建随机一次性 AppContainer profile 和合成目录。ACL 修改限这些目录；测试 junction 已移除，profile 由 DeleteAppContainerProfile 删除，HRESULT 0。合成结果保留在忽略目录用于诊断。

最终探针：`.agents/probes/lpac_relative_full_probe.py`。SHA-256：

```text
C79615105F12016BB98AA8E10715A590441E11E47EDE70ECE3B240A2F0E1A071
```

证据：`.agents/probes/lpac-6_vb9g82/evidence.json`。这些是本机实验文件，不随安装包交付。

## 启动机制与修正记录

使用 SECURITY_CAPABILITIES 与正确的 ALL_APPLICATION_PACKAGES_POLICY 属性，设置 ALL_APPLICATION_PACKAGES_OPT_OUT；额外能力为 registryRead、lpacCom，无网络 capability。使用 DETACHED_PROCESS、显式标准输出句柄列表及白名单环境；进程挂起创建，加入 kill-on-close Job 后才恢复。Job 负责进程树生命周期，不承担文件权限隔离。

**初版探针存在属性常量错误：0x2000e 是 CHILD_PROCESS_POLICY，正确值是 0x2000f（131087）。初版不得称为已验证的 LPAC。** AppContainer token 当时为真，但这不足以证明 LPAC 的额外限制。已按微软公开常量纠正，并补测仅向 ALL APPLICATION PACKAGES 放行的目录和后代访问均被拒绝。[Windows API 常量](https://microsoft.github.io/windows-docs-rs/doc/windows/Win32/System/Threading/constant.PROC_THREAD_ATTRIBUTE_ALL_APPLICATION_PACKAGES_POLICY.html)。

初版 CREATE_NO_WINDOW 退出 0xC0000142；改用 DETACHED_PROCESS 后固定 cmd 检查正常完成。授权目录通过明确工作目录下的相对路径访问成功；同轮绝对路径访问曾被拒绝，具体遍历原因没有进一步确认。没有以放开用户目录来解决。

cmd.exe 只执行宿主生成的固定测试脚本，**不是开放给模型的任意 shell**。

## 实测结果

| 检查 | 结果 |
| --- | --- |
| 授权目录写入 | 通过 |
| 授权材料读取 | 通过 |
| 拒绝读取相邻未授权目录 | 通过 |
| 拒绝写入相邻未授权目录 | 通过 |
| 拒绝修改只读材料 | 通过 |
| 拒绝 junction 越界读取 | 通过 |
| 拒绝仅向 ALL APPLICATION PACKAGES 放行的目录 | 通过 |
| 子进程写入授权目录 | 通过 |
| 子进程拒绝读取相邻目录 | 通过 |
| 子进程继承 LPAC 限制 | 通过，ALL APPLICATION PACKAGES 目录仍拒绝 |
| Job 终止后停止写入 | 通过，先确认写入已开始，再确认大小稳定 |
| Job 终止后活动进程数为零 | 通过，QueryInformationJobObject 返回 0 |

主进程 TokenIsAppContainer=true；检查命令退出码 0。不存在用未启动的进程或未变化的哨兵冒充拒绝成功。

## 部署含义与未覆盖项

当前账户可以创建 profile、授权自有合成目录并运行上述检查，无需本次提权。没有据此认定生产机器具备相同条件。

registryRead、lpacCom 及系统默认可访问对象是额外暴露面，需要随具体工具部署审计；不声称只可能访问清单中的文件。[微软 LPAC 说明](https://learn.microsoft.com/en-us/windows/win32/secauthz/implementing-an-appcontainer)、[官方测试工具](https://github.com/microsoft/SandboxSecurityTools/blob/main/LaunchAppContainer/README.md)。

未验收：Python/其他解释器、实际构建工具、任意项目脚本、网络、命名管道/控制服务、旧句柄与路径替换竞态、资源授权撤销、宿主崩溃恢复、安装包及普通 Windows。未授予网络能力不是完整网络隔离验收。

正式后端必须使用受控资源代理，审查每项检查工具及只读依赖，绑定操作/授权，并完成撤销和停止核对；不满足时拒绝能力。现有完整权限 Pi/插件 I/O 不能自动算入上述保证。

## 同日实现后的补充验证

原型之后已接入受控文件代理、授权控制、Pi 工具和不可变产物；当前接口见[受控执行接入](../development/controlled-execution.md)。新增原生测试覆盖固定脚本摘要拒绝、当前轮输入／产物目录、重复取消等待子进程停止。路径／硬链接／活动写句柄的拒绝由文件代理测试覆盖；不将它们扩大为全部 OS 竞争条件已验收。

源码及独立 wheel 安装目录均通过真实 Pi＋本地合成模型生命周期测试：多轮上下文、宿主重启后重新续租、在途撤销、迟到写入阻断、unknown 保留、核对后新上下文重试、共享用量、关闭和收集工作区后继续读取已发布产物。没有真实供应商调用，也没有部署到现有运行实例。

全量尝试 1,266 项；修正版本负例测试并补用缓存构建工具重测后，1,232 项通过、24 项跳过、10 项因缺依赖出错（6 个模块：environment、device_operations、reply_videos、video_recording、wake_words、windows_extensions）。缺少 pyhap、pyatv、imageio_ffmpeg、sherpa_onnx、kaldi_native_fbank、hid、miniaudio；不是通过或跳过。静态检查、内核类型检查、锁文件检查及实际 wheel 版本／资源检查通过。未实测项仍按上节和当前接入指南列明，不能据此开放任意工具链或宣称生产认证。
