import re

from app.models import MessageRoute

_TRIVIAL = re.compile(r"^(了解|りょ|おけ|ok|ありがとう|ありがと|thx|笑|w+|草|うん|はい|なるほど|👍|🙏)[!！。\s]*$", re.IGNORECASE)
_ORGANIZER = re.compile(
    r"(飲み|飲もう|飲み会|宴会|二次会|店探|お店探|居酒屋|焼肉屋|BBQ|バーベキュー|参加|行ける|行けない|"
    r"日程|候補日|何日|何時集合|今どんな感じ|予算.*円|場所.*(?:横浜|新宿|渋谷)|(?:月|日|時).*(?:空いて|いける|行ける))"
)
_ASSISTANT = re.compile(
    r"(\?|？|誰か知って|教えて|調べて|まとめて|どう思う|どうすれば|どうしたら|どっちが正しい|"
    r"これ何|何これ|分から|わから|できない|どこにある|噛み合って|話.*ズレ|合ってる|"
    r"天気|ニュース|営業時間|運行|遅延|価格|iPhone|Android|PC|スマホ|AI|幹事)"
)
_POTENTIAL_NEED = re.compile(
    r"((?:お腹|腹|小腹)(?:が)?(?:空|す)(?:い|いた|いて|いてきた|きた|く)|腹減った|腹へった|"
    r"なんか食べたい|何食べよう|ご飯どうしよう|ラーメン食べたい|"
    r"天気なんだろ|雨降りそう|雨かな|傘(?:いる|持ってくれば)|暑いかな|雪大丈夫|"
    r"遅刻しそう|間に合わな|電車.*(?:止ま|遅れ)|帰りの電車あるかな|"
    r"充電(?:やば|ない)|スマホ(?:死にそう|切れそう)|バッテリー(?:やば|ない)|"
    r"このあと(?:暇|どうしよう)|時間余った|駅.*(?:出口|わから|迷)|どっちから出|迷った|"
    r"眠い.*(?:運転|車)|暑すぎて.*(?:外|遊))"
)
_WEATHER_WORD = re.compile(r"(天気|雨|晴れ|雪|気温|暑(?:い|く)|寒(?:い|く)|傘)")
_WEATHER_QUESTION = re.compile(r"(かな(?:あ|ー*)?|だろ(?:う)?|なんだろ|どうだろ|かね|どう|何|いる|降る|晴れる|[?？])")
_LATENT_NEED_CUE = re.compile(
    r"(かな(?:あ|ー*)?|だろ(?:う)?|かも|気になる|困|悩|迷|分から|わから|決まら|できな|間に合わ|"
    r"足りな|なくな|ない|ほしい|欲しい|したい|しそう|すぎ(?:る|ない)?|やば|飽き|退屈|"
    r"高すぎ|安い|代わり|どうしよう|何しよう|[?？])"
)


class MessageRouter:
    def route(
        self,
        message: str,
        *,
        has_open_issues: bool = False,
        has_active_event: bool = False,
        has_active_topic: bool = False,
    ) -> MessageRoute:
        text = re.sub(r"\s+", "", message)
        if not text:
            return MessageRoute.NO_ACTION
        if _ORGANIZER.search(text):
            return MessageRoute.ORGANIZER
        if has_active_topic:
            return MessageRoute.CONVERSATION_ASSISTANT
        if _TRIVIAL.fullmatch(text):
            return MessageRoute.CONVERSATION_ASSISTANT if has_open_issues else MessageRoute.NO_ACTION
        if has_active_event and re.search(r"(行け|無理|空いて|場所|予算|横浜|新宿|渋谷|何時|何日|どんな感じ)", text):
            return MessageRoute.ORGANIZER
        return MessageRoute.CONVERSATION_ASSISTANT


def is_explicit_assistant_call(message: str, bot_name: str) -> bool:
    compact = re.sub(r"\s+", "", message)
    cues = (bot_name, "AI", "教えて", "調べて", "まとめて", "どう思う", "これ分かる", "これわかる")
    return any(cue and cue.lower() in compact.lower() for cue in cues)


def is_weather_candidate(message: str) -> bool:
    compact = re.sub(r"\s+", "", message)
    if not _WEATHER_WORD.search(compact):
        return False
    if re.search(r"(天気いいね|いい天気|晴れてよかった|雨すごかった)", compact):
        return False
    return bool(_WEATHER_QUESTION.search(compact) or re.search(r"(今日|明日|明後日|週末|今夜|午後|朝|夜)", compact))


def has_latent_need_signal(message: str) -> bool:
    compact = re.sub(r"\s+", "", message)
    return bool(_ASSISTANT.search(compact) or _POTENTIAL_NEED.search(compact) or _LATENT_NEED_CUE.search(compact))


def lightweight_need_signals(message: str) -> dict[str, bool]:
    compact = re.sub(r"\s+", "", message)
    return {
        "potential_need_language": has_latent_need_signal(compact),
        "explicit_question_mark": bool(re.search(r"[?？]", compact)),
        "statement_style": not bool(re.search(r"[?？]", compact)),
    }
