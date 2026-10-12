"""Only short, standalone device/session commands bypass the reasoning agent."""
from dataclasses import dataclass
import re


@dataclass
class Command:
    kind: str
    value: object = None


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


def parse_command(text):
    s = normalize(text)
    s = re.sub(r"^(请|麻烦)", "", s)
    if s in {"停", "停止", "停一下", "别说了", "停止播放", "暂停播报", "停止播报", "暂停播放"}:
        return Command("stop")
    if s in {"取消任务", "取消当前任务", "停止任务", "终止任务", "不用做了"}:
        return Command("cancel")
    if s in {"结束对话", "结束聊天", "结束会话", "结束当前会话", "回到待机", "退出对话", "再见", "拜拜"}:
        return Command("sleep")
    closing = re.sub(r"^(?:好的|好了|好|行|嗯|谢谢你|谢谢)", "", s)
    if re.fullmatch(r"(?:那|我们)?(?:先这样吧?|(?:今天)?就到这(?:里)?(?:吧)?|先聊到这(?:里)?(?:吧)?|(?:暂时|先)不聊了|回头再聊|下次再聊)(?:回头再聊|下次再聊)?", closing):
        return Command("sleep")
    if s in {"现在进度", "进度怎么样", "做完了吗", "任务进度", "现在做到哪了"}:
        return Command("progress")
    if re.fullmatch(r"(?:切换到?|换到?|使用|用)(客厅|书房)(?:音箱|播放|播报)?", s):
        return Command("room", "study" if "书房" in s else "living")
    if re.fullmatch(r"(?:在)?(客厅|书房)(?:音箱)?(?:播放|播报|回答)", s):
        return Command("room", "study" if "书房" in s else "living")
    if s in {"音量最大", "声音调到最大", "音量调到最大"}:
        return Command("volume", 100)
    if s in {"静音", "音量最小", "关闭声音"}:
        return Command("volume", 0)
    m = re.fullmatch(r"(?:把)?(?:音量|声音)(?:调到|设为|设置为|调成)?(?:百分之)?([0-9零一二三四五六七八九十百]+)(?:%|％)?", s)
    if m:
        v = chinese_number(m[1])
        if v is not None and 0 <= v <= 100:
            return Command("volume", v)
    return None

