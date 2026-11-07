# 终端协议 1 与模块边界

## 所有权

| 模块 | 所有权 |
|---|---|
| conversation | 设备独立会话、输入收据、澄清和引导原子检查 |
| Pi | 唯一执行循环、模型、工具和取消 |
| speech_io | 序号、音频量、结束输入和流清理 |
| aliyun_speech | 阿里协议、字数／秒数预留结算 |
| notifications | 原始任务通知、终端收到／播放／已读收据 |
| ai_passport | 设备身份、会话绑定、设备连接与播放版本 |
| app + configuration entrance | 私有控制通道、管理员操作和有界 WSS 代理 |
| firmware | 本地缓存、按键、显示、PCM、Wi-Fi、TLS、NVS |

扩展只通过声明的 PluginContext 访问公共能力。服务端绑定会话和来源，客户端不能
提交任意 session_id、模型、供应商密钥、执行身份或结果文件引用。

## 网络格式

配对 POST `/pair`：protocol=1、device_id、credential、pairing_token。成功回
`{"paired":true,"protocol":1}`。凭据哈希持久保存；同一未过期材料可恢复丢失的配对确认，
不能换绑身份。WebSocket `/stream` 使用 Authorization Bearer 与 X-Device-Id，
拒绝 Cookie 和 Origin，拒绝同一设备重复活动连接。

每条 JSON ≤16 KiB；主操作如下：

| 上行 | 下行 |
|---|---|
| input.begin(request_id, mode) | input.ready，绑定目标与共享工作额度 |
| 二进制：4 字节大端序号 + PCM16 | transcript.partial（仅显示） |
| input.finish(request_id, frames) | recognizing、transcript、input.receipt |
| input.cancel / input.receipt | 收据状态，不重发业务 |
| message.page(message_id, offset) | 80 字正文页，next_offset/null |
| playback.stop / playback.continue(message_id, offset) | 停止或开启新播放版本 |
| playback.credit(generation, bytes) | 二进制：4 字节大端播放版本 + PCM16 |
| playback.completed(generation) | 播放收据；下发完成不等于扬声器播放完成 |
| execution.cancel(request_id) | 现有会话取消结果 |
| notifications.list(before) | 8 条有归属的结果、复合游标 |
| notification.page / notification.listen(id, revision, offset) | 绑定结果分页／主动播放 |
| notification.receipt(id, revision, action) | received、played、read 独立记录 |

playback.start 携带 offset 和 next_offset；设备完整消费 PCM 后才推进继续听位置。
playback.stop 递增 generation 并清空缓冲，旧数据忽略。停止音频不取消已接受任务。
音频流错位、漏帧、超量或断线时，不用已有部分识别偷偷执行。

## 重连与失败

输入 ID 写入设备 NVS；服务器在业务派发前持久记录 unknown，返回后写 accepted。
重连只查询收据；对已进入会话队列的输入可以核实 accepted。引导已发出但确认丢失
时保留 unknown，不凭缺少活动执行推断未提交，也不换 ID 重发。

输入时捕获目标执行，提交前重新核对。目标已结束可成为新输入；目标已换成另一执行
或澄清问题已变化时，返回明确状态要求用户重新说，不补充到不相关执行。
设备断线不取消服务器任务，恢复只同步文字和结果清单，旧音频不自动补播。

ASR 失败不进入执行；TTS 失败保留文本和业务结果。没有任何供应商回退、业务重做或
未知操作自动恢复。用量准入异常直接阻止付费请求；原始音频和正文不进入普通日志。

## 扩展

识别和合成分别对应 RecognitionRequest 和 SynthesisRequest。新适配可以只登记其中一个，
阿里适配 capabilities 也可分别关闭。不会复制模型执行、任务调度或通知数据库。
