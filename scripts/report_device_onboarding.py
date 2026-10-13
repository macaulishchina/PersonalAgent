"""Generate the hardware onboarding acceptance report from private evidence."""

from datetime import datetime, timezone
import html
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    folder = ROOT / "state/acceptance/device-onboarding"
    real = json.loads((folder / "real-devices.json").read_text())
    restart = json.loads((folder / "production-restart.json").read_text())
    assert real["passed"] and restart["passed"]

    def esc(value):
        return html.escape(str(value))

    rows = "".join(
        f"<tr><td>{esc(d['label'])}<small>{esc(d['id'])}</small></td>"
        f"<td>{esc(d['kind'])}</td><td>{esc('、'.join(d['capabilities']))}</td>"
        f"<td>{esc('可用' if d['state'] == 'available' else '不可用')}</td></tr>"
        for d in restart["devices"]
    )
    acoustic = real["acoustic_loop"]
    checked = datetime.fromtimestamp(restart["checked_at"], timezone.utc).isoformat()
    doc = """<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>硬件标准接入验收 · Personal Agent</title><style>
    *{box-sizing:border-box}body{margin:0;background:#f4f6f8;color:#162c3c;font:16px/1.75 system-ui,sans-serif}main{max-width:1040px;margin:auto;padding:32px 20px}h1{font-size:clamp(28px,5vw,44px);line-height:1.3}h2{font-size:24px}section,.hero{background:white;border:1px solid #dce3e7;border-radius:18px;padding:26px;margin:20px 0}.pill{display:inline-block;background:#d9f0e5;color:#146640;padding:5px 12px;border-radius:30px;font-weight:700}.flow,.cards{display:grid;grid-template-columns:repeat(3,1fr);gap:12px}.step,.card{background:#eaf0f4;border-radius:12px;padding:17px}.card b{display:block;font-size:28px;color:#166848}small{display:block;color:#647988}.table{overflow-x:auto}table{width:100%;border-collapse:collapse}th,td{padding:12px;text-align:left;border-bottom:1px solid #dce3e7}pre{background:#142a38;color:#e0f1fc;padding:18px;border-radius:10px;white-space:pre-wrap;overflow-wrap:anywhere}a{color:#216ec1}.note{color:#647988}li{margin:9px 0}@media(max-width:620px){.flow,.cards{grid-template-columns:1fr}section,.hero{padding:18px}}
    </style><main><div class="hero"><span class="pill">已正式运行 · 验收通过</span><h1>麦克风和客厅音箱<br>按统一规范接入</h1><p>设备定义、能力注册、状态查询、AI 工具和真实硬件测试使用同一条接入链路。今后同协议设备主要通过配置添加，新协议由一个适配器接入。</p><p class="note">核实时间：__CHECKED__；本页是验收快照，不是实时设备监控。</p></div>
    <section><h2>标准接入流程</h2><div class="flow"><div class="step">① 配置设备<br><b>稳定 ID / 名称 / 位置 / 连接参数</b></div><div class="step">② 适配器验证<br><b>协议参数、唯一身份、生命周期</b></div><div class="step">③ 注册能力<br><b>设备目录统一管理</b></div><div class="step">④ 返回状态<br><b>在线、选中、时间与有效期</b></div><div class="step">⑤ AI 查询使用<br><b>工具 → 能力 → 适配器</b></div><div class="step">⑥ 实物验收<br><b>真实采集、出声、恢复</b></div></div></section>
    <section><h2>现在登记了哪些设备</h2><div class="table"><table><tr><th>设备</th><th>类型</th><th>能力</th><th>验收时状态</th></tr>__ROWS__</table></div><p class="note">书房音箱沿用同一配置规范，但本轮实物验收对象是摄像头麦克风与客厅音箱。书房不可用时仍保留明确身份，不用另一设备冒充。</p></section>
    <section><h2>不是只检查“程序说播放成功”</h2><div class="cards"><div class="card"><b>__CORR__</b>音箱测试提示音与真实麦克风信号的相关系数</div><div class="card"><b>__SECONDS__ 秒</b>现场采集长度；原始音频仅在内存中处理，没有保存</div><div class="card"><b>3 项</b>真实 Codex 工具调用：列表、麦克风查询、客厅音箱查询</div></div><p>测试在隔离会话中进行，播放既有提示音并通过物理麦克风检测到了它；未启用语音对话，也未保存自动记忆。通过蓝牙主动断开客厅音箱后，目录返回不可用；重连恢复后返回可用。</p></section>
    <section><h2>已验证的项目</h2><ul><li>配置校验、唯一设备匹配、登记归属、禁用设备与未知设备处理。</li><li>状态时间/有效期、旧状态隔离、探测失败和调用超时。</li><li>真实麦克风采集与客厅音箱声学出声确认。</li><li>蓝牙断连恢复，客厅不可用时不改播到书房。</li><li>Codex → MCP → 管理 socket → 设备目录 → 适配器的真实查询。</li><li>正式服务重启后设备重新登记，麦克风与音频路径正常；原会话和任务记录未改变。</li><li>88 项开发测试通过，代码格式、静态检查和内核类型检查通过。</li></ul><p class="note">未声称完成 USB 实际拔插、整机重启或 24 小时稳定性验收。不同协议的硬件仍需实现对应适配器；新执行动作需定义具体能力，声明设备不会自动生成驱动。</p></section>
    <section><h2>以后如何查看和复测</h2><pre>.venv/bin/personal-agent --config config.toml devices list
.venv/bin/personal-agent --config config.toml devices inspect living_microphone
.venv/bin/personal-agent --config config.toml devices inspect living
.venv/bin/python -m scripts.verify_device_onboarding --config config.toml --disconnect</pre><p>最后一条是明确实物测试命令：要求服务空闲，会短暂播放提示音、断连重连音箱，并在结束后恢复正式服务。</p><p><a href="../state/acceptance/device-onboarding/real-devices.json">实物验收 JSON</a> · <a href="../state/acceptance/device-onboarding/production-restart.json">正式服务重启验收 JSON</a> · <a href="../src/assistant_runtime/plugins/pipewire/README.md">适配器接入说明</a></p></section></main></html>"""
    resource_file = folder / "resource-smoke.json"
    if resource_file.exists():
        resource = json.loads(resource_file.read_text())
        panel = (
            "<section><h2>常驻资源与待机检查</h2><p>"
            + f"实测 {resource['duration_seconds']:.1f} 秒，主进程整机 CPU 占比 {resource['host_machine_cpu_percent']:.2f}%，"
            + f"RSS 从 {resource['host_rss_start_mib']:.1f} 到 {resource['host_rss_end_mib']:.1f} MiB。"
            + "期间反复查询设备，唤醒次数保持 0，服务保持待机。"
            + '</p><p class="note">这是包含本地语音模型和音频处理的主进程短时测量，不是设备观察器的独立成本；不含 Codex/Basic Memory 子进程，不代表 24 小时稳定性。</p>'
            + '<p><a href="../state/acceptance/device-onboarding/resource-smoke.json">资源检查 JSON</a></p></section>'
        )
        doc = doc.replace("</main>", panel + "</main>")
    for key, value in {
        "CHECKED": checked,
        "ROWS": rows,
        "CORR": f"{acoustic['chime_correlation']:.3f}",
        "SECONDS": f"{acoustic['capture_seconds']:.2f}",
    }.items():
        doc = doc.replace(f"__{key}__", value if key == "ROWS" else esc(value))
    target = ROOT / "reports/DEVICE_ONBOARDING.html"
    target.parent.mkdir(exist_ok=True)
    target.write_text(doc)
    print(target)


if __name__ == "__main__":
    main()
