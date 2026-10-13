"""Only short, standalone device/session commands bypass the reasoning agent."""

from ..kernel.contracts import Control
import re


def normalize(text):
    return re.sub(r"[\s，。！？、,.!?]", "", text).lower()


def chinese_number(text):
    if text.isdigit():
        return int(text)
    digits = dict(zip("零一二三四五六七八九", range(10)))
    if text in ("一百", "百"):
        return 100
    if re.fullmatch(r"[一二三四五六七八九]?十[一二三四五六七八九]?", text):
        a, b = text.split("十", 1)
        return (digits.get(a, 1) * 10) + digits.get(b, 0)
    return digits.get(text)


def parse_command(text, room_labels=None, wake_words=()):
    s = normalize(text)
    for word in sorted((normalize(word) for word in wake_words), key=len, reverse=True):
        if word and s.startswith(word):
            s = s[len(word) :]
            break
    s = re.sub(r"^(请|麻烦)", "", s)
    if s in {"停", "停止", "停一下", "别说了", "停止播放", "暂停播报", "停止播报", "暂停播放"}:
        return Control("stop")
    if s in {"取消任务", "取消当前任务", "停止任务", "终止任务", "不用做了"}:
        return Control("cancel")
    if s in {"结束对话", "结束聊天", "结束会话", "结束当前会话", "回到待机", "退出对话", "再见", "拜拜"}:
        return Control("sleep")
    closing = re.sub(r"^(?:好的|好了|好|行|嗯|谢谢你|谢谢)", "", s)
    if re.fullmatch(
        r"(?:那|我们)?(?:先这样吧?|(?:今天)?就到这(?:里)?(?:吧)?|先聊到这(?:里)?(?:吧)?|(?:暂时|先)不聊了|回头再聊|下次再聊)(?:回头再聊|下次再聊)?",
        closing,
    ):
        return Control("sleep")
    if s in {"现在进度", "进度怎么样", "做完了吗", "任务进度", "现在做到哪了"}:
        return Control("progress")
    matching_outputs = set()
    for ident, labels in (room_labels or {}).items():
        for label in [labels] if isinstance(labels, str) else labels:
            room = re.escape(normalize(label))
            if re.fullmatch(rf"(?:切换到?|换到?|使用|用)({room})(?:音箱|播放|播报)?", s) or re.fullmatch(
                rf"(?:在)?({room})(?:音箱)?(?:播放|播报|回答)", s
            ):
                matching_outputs.add(ident)
    if len(matching_outputs) == 1:
        return Control("room", matching_outputs.pop())
    if matching_outputs:
        return None  # Delegate ambiguous room names; never choose the first device.
    if s in {"音量最大", "声音调到最大", "音量调到最大"}:
        return Control("volume", 100)
    if s in {"静音", "音量最小", "关闭声音"}:
        return Control("volume", 0)
    m = re.fullmatch(
        r"(?:把)?(?:音量|声音)(?:调到|设为|设置为|调成)?(?:百分之)?([0-9零一二三四五六七八九十百]+)(?:%|％)?",
        s,
    )
    if m:
        v = chinese_number(m[1])
        if v is not None and 0 <= v <= 100:
            return Control("volume", v)
    return None
