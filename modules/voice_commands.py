"""
modules/voice_commands.py

语音指令解析层。

只做一件很窄的事：把识别出来的文本**归一化**后，和预先定义的指令短语比对，
判断这句话是不是一条控制指令。具体动作由调用方（app.py）注册，这个模块不碰摄像头。

为什么单独拆一层：
1. 纯字符串逻辑，可以脱离麦克风和模型单测
2. 指令表集中在一处，后续要加「打开灯光」「拍照」之类只需在这里加一条
3. 匹配可以做得足够保守，避免背景人声里偶然出现的词触发误操作

匹配策略（保守优先）：
- 先去掉空格和标点（vosk 输出是「打开 摄像头」这种带空格的词串）
- 短语必须出现在句首、句尾或整句，且多出来的字不超过 `TOLERANCE` 个
  —— 这样「请打开摄像头」「打开摄像头吧」能命中，而别人聊天里随口提到的
  「我待会儿去关掉那个摄像头再试试」不会误触发
"""
from __future__ import annotations

import re

# 允许句子里比短语多出几个字（语气词、礼貌用语），超出就不认
TOLERANCE = 2

# 去掉两边空白后仍然要剔除的字符：空格和常见中英文标点
_STRIP_PATTERN = re.compile(r"[\s,，。.、!！?？;；:：\"'“”‘’()（）《》<>\[\]【】\-—…~·]+")

# 指令表：id 用于调用方分发，phrases 是归一化后的短语
COMMANDS: list[dict] = [
    {
        "id": "camera_on",
        "description": "打开摄像头",
        "phrases": (
            "打开摄像头",
            "开启摄像头",
            "启动摄像头",
            "打开相机",
            "开摄像头",
            "摄像头打开",
        ),
    },
    {
        "id": "camera_off",
        "description": "关闭摄像头",
        "phrases": (
            "关闭摄像头",
            "关掉摄像头",
            "停止摄像头",
            "关闭相机",
            "关摄像头",
            "摄像头关闭",
        ),
    },
]


def normalize(text: str) -> str:
    """去掉空白与标点，得到便于比对的字符串。"""
    if not text:
        return ""
    return _STRIP_PATTERN.sub("", text)


def match(text: str) -> dict | None:
    """判断文本是否为一条指令。

    命中时返回 {"id", "description", "phrase", "text", "normalized"}，否则返回 None。
    """
    normalized = normalize(text)
    if not normalized:
        return None

    for command in COMMANDS:
        for phrase in command["phrases"]:
            if _contains_as_command(normalized, phrase):
                return {
                    "id": command["id"],
                    "description": command["description"],
                    "phrase": phrase,
                    "text": text,
                    "normalized": normalized,
                }
    return None


def _contains_as_command(normalized: str, phrase: str) -> bool:
    """短语是否以「指令」的方式出现在句子里。

    允许三种形态（多余字符总数不超过 TOLERANCE）：
    - 整句就是短语
    - 短语在句首（如「打开摄像头吧」）
    - 短语在句尾（如「请打开摄像头」）
    - 短语前后各多出 1 个字（如「请打开摄像头吧」）

    长句里顺口提到不会被认成指令，例如「把摄像头打开一点角度吧」。
    """
    if normalized == phrase:
        return True
    index = normalized.find(phrase)
    if index < 0:
        return False

    prefix = index
    suffix = len(normalized) - index - len(phrase)
    if prefix + suffix > TOLERANCE:
        return False
    # 短语必须靠前或靠后，不能藏在句子中间
    return prefix <= 1 or suffix <= 1


def describe_commands() -> list[dict]:
    """返回指令说明，供前端/接口展示。"""
    return [
        {"id": c["id"], "description": c["description"], "phrases": list(c["phrases"])}
        for c in COMMANDS
    ]


class VoiceCommandRouter:
    """把识别文本路由到具体动作。

    调用方注册 `actions = {"camera_on": fn, "camera_off": fn}`，
    动作函数自己负责真正做事（启停摄像头），本类只负责匹配、执行、广播结果。
    """

    def __init__(self, socketio, actions: dict):
        self.socketio = socketio
        self.actions = dict(actions)
        self.last_result: dict | None = None

    def handle(self, text: str) -> dict | None:
        """文本命中指令时执行并返回结果；不是指令则返回 None（调用方无需理会）。"""
        from datetime import datetime

        hit = match(text)
        if hit is None:
            return None

        result = {
            "timestamp": datetime.now().isoformat(),
            "text": text,
            "command": hit["id"],
            "description": hit["description"],
            "phrase": hit["phrase"],
        }
        action = self.actions.get(hit["id"])
        if action is None:
            result.update({"ok": False, "error": "没有注册该指令的处理函数"})
        else:
            try:
                detail = action()
                result.update({"ok": True, "detail": detail})
            except Exception as exc:
                result.update({"ok": False, "error": f"{type(exc).__name__}: {exc}"})

        self.last_result = result
        status = "成功" if result["ok"] else f"失败({result.get('error')})"
        print(f"[Voice] 「{text}」 -> {hit['description']} {status}")
        try:
            self.socketio.emit("voice_command", result)
        except Exception as exc:
            print(f"[Voice] 广播指令结果失败: {exc}")
        return result

    def get_last_result(self) -> dict:
        return dict(self.last_result) if self.last_result else {}

