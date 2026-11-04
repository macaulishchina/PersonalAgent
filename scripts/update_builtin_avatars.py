"""Explicit maintenance of the packaged avatar set; never used at runtime.

Brand SVGs are pinned to Lobe Icons; general glyphs use the existing Lucide dependency.
Run from the repository after reviewing the source commit and licenses.
"""

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import re
import subprocess
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
SOURCE_COMMIT = "ea434b96e75813517b377825e01c0df61dee8836"
SOURCE = f"https://raw.githubusercontent.com/lobehub/lobe-icons/{SOURCE_COMMIT}/"
BRANDS = [
    ("openai", "OpenAI", "openai"),
    ("anthropic", "Anthropic / Claude", "anthropic"),
    ("google", "Google / Gemini", "gemini"),
    ("meta", "Meta / Llama", "meta"),
    ("mistral", "Mistral AI", "mistral"),
    ("xai", "xAI / Grok", "xai"),
    ("cohere", "Cohere", "cohere"),
    ("perplexity", "Perplexity", "perplexity"),
    ("huggingface", "Hugging Face", "huggingface"),
    ("microsoft", "Microsoft / Copilot", "copilot"),
    ("nvidia", "NVIDIA", "nvidia"),
    ("amazon", "Amazon / AWS", "aws"),
    ("groq", "Groq", "groq"),
    ("ai21", "AI21 Labs", "ai21"),
    ("openrouter", "OpenRouter", "openrouter"),
    ("deepseek", "DeepSeek 深度求索", "deepseek"),
    ("qwen", "阿里通义 / Qwen", "qwen"),
    ("kimi", "月之暗面 / Kimi", "kimi"),
    ("zhipu", "智谱 / GLM", "zhipu"),
    ("minimax", "MiniMax", "minimax"),
    ("doubao", "字节豆包 / Doubao", "doubao"),
    ("baidu", "百度文心", "baidu"),
    ("tencent", "腾讯混元 / Hunyuan", "hunyuan"),
    ("stepfun", "阶跃星辰 / StepFun", "stepfun"),
    ("baichuan", "百川智能", "baichuan"),
    ("iflytek", "讯飞星火", "spark"),
    ("sensetime", "商汤日日新", "sensenova"),
    ("yi", "零一万物 / Yi", "yi"),
    ("internlm", "书生 / InternLM", "internlm"),
    ("siliconflow", "硅基流动 / SiliconFlow", "siliconcloud"),
]
GENERAL = [
    ("robot", "机器人", "Bot"),
    ("sparkles", "灵感", "Sparkles"),
    ("star", "星星", "Star"),
    ("sun", "太阳", "Sun"),
    ("moon", "月亮", "Moon"),
    ("leaf", "绿叶", "Leaf"),
    ("flower", "花朵", "Flower2"),
    ("mountain", "山峰", "Mountain"),
    ("waves", "海浪", "Waves"),
    ("cloud", "云朵", "Cloud"),
    ("flame", "火焰", "Flame"),
    ("snowflake", "雪花", "Snowflake"),
    ("gem", "宝石", "Gem"),
    ("hexagon", "六边形", "Hexagon"),
    ("orbit", "轨道", "Orbit"),
    ("planet", "行星", "Globe"),
    ("rocket", "火箭", "Rocket"),
    ("compass", "指南针", "Compass"),
    ("lightbulb", "灯泡", "Lightbulb"),
    ("book", "书本", "BookOpen"),
    ("code", "代码", "CodeXml"),
    ("terminal", "终端", "SquareTerminal"),
    ("chart", "图表", "ChartNoAxesCombined"),
    ("shield", "盾牌", "Shield"),
    ("gear", "齿轮", "Settings"),
    ("puzzle", "拼图", "Puzzle"),
    ("music", "音乐", "Music"),
    ("camera", "相机", "Camera"),
    ("palette", "调色盘", "Palette"),
    ("coffee", "咖啡", "Coffee"),
    ("cat", "小猫", "Cat"),
    ("dog", "小狗", "Dog"),
    ("rabbit", "兔子", "Rabbit"),
    ("bird", "小鸟", "Bird"),
    ("fish", "小鱼", "Fish"),
    ("bug", "甲虫", "Bug"),
    ("heart", "爱心", "Heart"),
    ("beaker", "烧杯", "Beaker"),
    ("microscope", "显微镜", "Microscope"),
    ("anchor", "船锚", "Anchor"),
]


def fetch(relative):
    req = urllib.request.Request(SOURCE + relative, headers={"User-Agent": "PA-avatar-maintenance"})
    with urllib.request.urlopen(req, timeout=20) as response:
        data = response.read(128 * 1024 + 1)
    if len(data) > 128 * 1024:
        raise ValueError("Icon exceeds bound")
    return data


def brand(item):
    name, label, source = item
    for suffix in ("-color.svg", ".svg"):
        relative = "packages/static-svg/icons/" + source + suffix
        try:
            raw = fetch(relative)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                continue
            raise
        svg = ET.fromstring(raw)
        if re.search(
            rb"<(script|foreignObject)\b|\bon\w+\s*=|(?:href|src)\s*=|https?://(?!www.w3.org)", raw, re.I
        ):
            raise ValueError("Unsafe upstream SVG: " + name)
        svg.set("x", "10")
        svg.set("y", "10")
        svg.set("width", "44")
        svg.set("height", "44")
        svg.set("color", "#172b24")
        outer = ET.Element(
            "{http://www.w3.org/2000/svg}svg", {"width": "64", "height": "64", "viewBox": "0 0 64 64"}
        )
        ET.SubElement(
            outer,
            "{http://www.w3.org/2000/svg}rect",
            {
                "x": ".5",
                "y": ".5",
                "width": "63",
                "height": "63",
                "rx": "18",
                "fill": "#fff",
                "stroke": "#e4e7ec",
            },
        )
        outer.append(svg)
        return {"name": name, "label": label, "group": "providers", "source": relative}, ET.tostring(
            outer, encoding="utf-8"
        )
    raise ValueError("No pinned SVG for " + name + " (" + source + ")")


def main():
    ET.register_namespace("", "http://www.w3.org/2000/svg")
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(brand, BRANDS))
    script = r"""
const React = require('./frontend/node_modules/react');
const {renderToStaticMarkup} = require('./frontend/node_modules/react-dom/server');
const icons = require('./frontend/node_modules/lucide-react');
const items = JSON.parse(process.argv[1]);
const colors = [['#e7f1eb','#29664a'],['#e7eefb','#365c9c'],['#f1eafb','#78549f'],['#f9e9ed','#a64c69'],['#fbefdf','#996428'],['#e5f2f3','#337980'],['#edf0e2','#687a35'],['#e9ecf2','#53647c']];
const result = items.map(([name,label,icon],i)=>{
 if(!icons[icon]) throw Error('Missing Lucide icon '+icon);
 const glyph=renderToStaticMarkup(React.createElement(icons[icon],{strokeWidth:1.7})).replace(/^<svg[^>]*>/,'').replace(/<\/svg>$/,'');
 const [bg,fg]=colors[i%colors.length];
 const svg=`<svg xmlns="http://www.w3.org/2000/svg" width="64" height="64" viewBox="0 0 64 64"><rect width="64" height="64" rx="18" fill="${bg}"/><g transform="translate(16 16) scale(1.333333)" fill="none" stroke="${fg}" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round">${glyph}</g></svg>`;
 return {name,label,icon,svg};
});
process.stdout.write(JSON.stringify(result));
"""
    data = subprocess.check_output(["node", "-e", script, json.dumps(GENERAL, ensure_ascii=True)], cwd=ROOT)
    glyphs = json.loads(data)
    results += [
        (
            {
                "name": item["name"],
                "label": item["label"],
                "group": "general",
                "source": "lucide:" + item["icon"],
            },
            item["svg"].encode(),
        )
        for item in glyphs
    ]
    robot = next(raw for item, raw in results if item["name"] == "robot")
    results.insert(
        len(BRANDS),
        ({"name": "generic", "label": "默认助手", "group": "general", "source": "lucide:Bot"}, robot),
    )
    target = ROOT / "src/assistant_runtime/core/models/avatars"
    target.mkdir(parents=True, exist_ok=True)
    manifest = []
    for item, raw in results:
        ET.fromstring(raw)
        (target / (item["name"] + ".svg")).write_bytes(raw)
        manifest.append({**item, "sha256": hashlib.sha256(raw).hexdigest()})
    (target / "LOBE_ICONS_LICENSE.txt").write_bytes(fetch("LICENSE"))
    (target / "LUCIDE_LICENSE.txt").write_bytes(
        (ROOT / "frontend/node_modules/lucide-react/LICENSE").read_bytes()
    )
    lucide = json.loads(
        (ROOT / "frontend/node_modules/lucide-react/package.json").read_text(encoding="utf-8")
    )
    catalog = {"source_commit": SOURCE_COMMIT, "lucide_version": lucide["version"], "avatars": manifest}
    (target / "catalog.json").write_text(
        json.dumps(catalog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    schema_path = ROOT / "src/assistant_runtime/core/models/configuration.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    names = "|".join(item["name"] for item in manifest)
    for group in ("providers", "entries"):
        schema["schema"]["properties"][group]["additionalProperties"]["properties"]["presentation"][
            "properties"
        ]["avatar"]["pattern"] = "^(builtin:(" + names + ")|asset:[a-f0-9]{64})$"
    schema_path.write_text(json.dumps(schema, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Packaged {len(BRANDS)} brand icons and {len(GENERAL)} general avatars (+ generic)")


if __name__ == "__main__":
    main()
