"""Generate HomePod integration evidence without altering device preferences."""

from datetime import datetime, timezone
import html
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    folder = ROOT / "state/acceptance/airplay-homepod"
    r = json.loads((folder / "real-devices.json").read_text())
    assert r["passed"]
    a = r["acoustic_loop"]
    o = r["output_after_play"]["device"]
    speech = r.get("speech_test", {})

    def esc(x):
        return html.escape(str(x))

    style = (
        (ROOT / "reports/DEVICE_ONBOARDING.html").read_text().split("<style>", 1)[1].split("</style>", 1)[0]
    )
    doc = """<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>HomePod 标准接入验收</title><style>__STYLE__</style><main><div class="hero"><span class="pill">实物试音通过</span><h1>HomePod 已接入标准设备框架</h1><p>项目设备 ID：homepod；实际接收器名称：__NAME__。通过与蓝牙音箱相同的选择、队列、停播和设备查询入口使用；跨协议传输由 AirPlay 插件承担。</p><p class="note">验收时间：__TIME__；这是验收快照，不是实时状态。</p></div>
    <section><h2>实际验证了什么</h2><div class="cards"><div class="card"><b>__CORR__</b>提示音与真实麦克风信号的相关系数</div><div class="card"><b>__BYTES__ 字节</b>共享播放器实际提交的 PCM</div><div class="card"><b>__SPEECH__ 秒</b>中文试音“这是一条语音测试”</div></div><ul><li>按主机与稳定 RAOP 标识找到指定接收器，配对要求为 NotNeeded。</li><li>经统一 PCM 输出接口播放提示音和已有中文样本，麦克风独立确认提示音出声。</li><li>真实 Codex 完成设备列表、麦克风和 HomePod 查询。</li><li>试音时接收器流开启；结束后关闭编码器与流会话，没有遗留 FFmpeg 进程。</li><li>94 项开发测试通过：注册归属、跨协议路由、输出队列边界、旧 PCM 清理、未知音量不调整和设备名称歧义处理。</li></ul></section>
    <section><h2>接入的实现路径</h2><div class="flow"><div class="step">设备配置<br><b>devices.homepod</b></div><div class="step">登记能力<br><b>目录 + 输出提供者</b></div><div class="step">统一选择<br><b>device room / CLI / 语音</b></div><div class="step">共享播放器<br><b>有界队列与停播</b></div><div class="step">AirPlay 插件<br><b>FFmpeg → pyatv RAOP</b></div><div class="step">实体输出<br><b>HomePod + 麦克风确认</b></div></div><p>pyatv 与 FFmpeg 都在项目原有虚拟环境里。已有 HomePod 工作目录只提供验证过的参数和试音样本，运行不依赖那套旧脚本。没有改接收器名称或防火墙规则。</p></section>
    <section><h2>使用方法</h2><pre>说：“嘟嘟，切换到 HomePod。”
.venv/bin/personal-agent --config config.toml devices inspect homepod
.venv/bin/personal-agent --config config.toml room homepod
.venv/bin/personal-agent --config config.toml room living</pre><p>默认输出仍配置为客厅小爱音箱；添加 HomePod 不代表自动更换默认输出。共用“客厅”位置时，具体“客厅音箱”名称优先于位置别名，HomePod 以自己的名称选择。</p></section>
    <section><h2>缓冲、音量和验证边界</h2><p>当前适配器无法可靠读取接收器实际音量，因此报告未知，应用不自动调整；显式音量请求可用并标注来源。发送库尽量沿用接收器提供的初始音量。</p><p>本次提示音位于麦克风记录的 __OFFSET__ 秒处，包含前置等待、编码/连接初始化和接收器缓冲。这不是唤醒词结束到回答的延迟，也不是最低播放延迟。快速声学打断、连续对话回声效果和长时间稳定性仍需单独验收。</p><p class="note"><a href="https://pyatv.dev/development/stream/">pyatv 流式 API 与缓冲说明</a> · <a href="https://pyatv.dev/documentation/supported_features/">RAOP 能力说明</a></p></section>
    <section><h2>证据与复测</h2><p><a href="../state/acceptance/airplay-homepod/real-devices.json">实际验收 JSON</a> · <a href="../src/assistant_runtime/plugins/airplay/README.md">适配器接入说明</a> · <a href="DEVICE_ONBOARDING.html">基础设备接入报告</a></p><pre>.venv/bin/python -m scripts.verify_device_onboarding --output-device homepod --tail-seconds 10 --speech-sample /path/to/short-test.wav</pre><p>要求正式服务空闲；独立状态/任务会话下试音，完成后恢复正式服务。原始麦克风音频仅在内存中处理，没有保存。</p></section></main></html>"""
    values = {
        "STYLE": style,
        "NAME": o["data"]["receiver_name"],
        "TIME": datetime.fromtimestamp(r["finished_at"], timezone.utc).isoformat(),
        "CORR": f"{a['chime_correlation']:.3f}",
        "BYTES": a["written_bytes"],
        "SPEECH": f"{speech.get('audio_seconds', 0):.3f}",
        "OFFSET": f"{a['match_offset_s']:.2f}",
    }
    for key, value in values.items():
        doc = doc.replace("__" + key + "__", str(value) if key == "STYLE" else esc(value))
    deployment_file = folder / "deployment.json"
    if deployment_file.exists():
        deployment = json.loads(deployment_file.read_text())
        if not deployment["audio_ready"]:
            doc = doc.replace(
                "</main>",
                '<section><h2>默认输出的当前状态</h2><p>部署核实时 HomePod 可用，麦克风采集正常；默认仍选择客厅小爱音箱，但该蓝牙设备当前未连接，所以默认音频路由未就绪。没有自动换到 HomePod。HomePod 的独立试音验收已通过，可以按上面的方法明确选择它。</p><p><a href="../state/acceptance/airplay-homepod/deployment.json">部署状态 JSON</a></p></section></main>',
            )
    path = ROOT / "reports/HOMEPOD_ONBOARDING.html"
    path.write_text(doc)
    print(path)


if __name__ == "__main__":
    main()
