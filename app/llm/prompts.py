"""Built-in default prompts and the placeholder contract.

The prompts are Chinese because the panel is; the code around them stays
English like the rest of `app/`.

The JSON field names each prompt asks for ARE the contract that the scenario's
Pydantic model validates (T3/T4). Rename a field in one place and the other
stops parsing, which the retry cannot fix — change both together.
"""

from collections.abc import Mapping

from app.llm.base import LlmConfigError

# Listed verbatim on the config page, so a user editing a template knows what
# is available. Anything not in this tuple is left as literal text.
PLACEHOLDERS: dict[str, tuple[str, ...]] = {
    "market": ("keyword", "window_days", "stats"),
    "item": ("keyword", "user_intent", "item", "price_history", "seller", "stats"),
}

# Appended by the caller, not requested from the model: a fixed sentence is
# fixed, and a sentence the model was asked to include is optional in practice.
DISCLAIMER = "以上内容由 AI 依据本工具采集的统计数据推断得出，仅供参考，不构成投资或交易建议。"

MARKET_PROMPT = """你是二手交易平台的行情分析助手。下面是关键词「{keyword}」最近 {window_days} 天
的聚合统计数据。你看不到原始商品列表，只能依据这些统计量作答，不要编造未给出的事实。

{stats}

请只输出一个 JSON 对象，不要输出解释文字，不要用 markdown 代码块包裹：
{"level": "偏低|正常|偏高", "trend": "下行|平稳|上行", "advice": "等待|观望|出手",
 "reasons": ["从上面某个统计量出发的一句话理由"], "summary": "两三句话的结论"}

要求：
- level 说的是当前挂牌价相对该窗口分位数的水位。
- 每条 reason 必须指向上面给出的某个具体数字。
- 数据不足以判断时，如实写进 summary，不要用模糊说法凑一个结论。"""

ITEM_PROMPT = """你是二手交易平台的捡漏助手。下面是一件商品的资料、它的价格变化、卖家画像，
以及关键词「{keyword}」的同期行情统计。用户的关注点是：{user_intent}

商品：
{item}

价格历史：
{price_history}

卖家：
{seller}

同期行情：
{stats}

如果附带了图片，请一并参考图片判断成色与真伪风险；没有图片时不要假设图片内容。

请只输出一个 JSON 对象，不要输出解释文字，不要用 markdown 代码块包裹：
{"verdict": "值得|可考虑|不建议", "fair_price_yuan": 合理价格数字, "offer_price_yuan": 建议出价数字,
 "risks": ["一句话风险点"], "summary": "两三句话的结论"}

要求：
- 两个价格都用人民币元的数字，不要带单位、不要写区间。
- risks 只写资料里有依据的风险，例如价格明显低于分位数、卖家新注册、描述与标题不一致。
- 只依据给出的资料作答，缺什么就在 summary 里说缺什么。"""

DEFAULT_PROMPTS: dict[str, str] = {"market": MARKET_PROMPT, "item": ITEM_PROMPT}


# The placeholders whose absence makes a template actively harmful rather than
# merely degraded. Everything else can go missing and the answer just gets
# thinner; drop one of these and the surrounding prose still says "下面是聚合
# 统计数据" and "每条 reason 必须指向上面给出的某个具体数字" while carrying no
# data at all -- a prompt that pressures the model to invent the numbers it was
# not given, and bills full price for the answer.
#
# Measured: deleting `{stats}` from the market template took the rendered
# prompt from 1626 characters to 352, keeping both of those sentences.
LOAD_BEARING: dict[str, tuple[str, ...]] = {
    "market": ("stats",),
    "item": ("item",),
}


def missing_placeholders(scenario: str, template: str) -> tuple[str, ...]:
    """Load-bearing placeholders this template dropped.

    The original design asked for "渲染时缺失占位符要报可读错误，不静默塞空
    字符串". `render` deliberately does not raise -- a typo'd `{plcaeholder}`
    should survive as visible text rather than 500 on someone mid-edit -- so
    the check lives here, where the caller can refuse to spend money instead
    of failing a keystroke.
    """
    return tuple(name for name in LOAD_BEARING.get(scenario, ()) if f"{{{name}}}" not in template)


def default_prompt(scenario: str) -> str:
    """The built-in template for a scenario. Raises LlmConfigError if unknown."""
    try:
        return DEFAULT_PROMPTS[scenario]
    except KeyError as exc:
        raise LlmConfigError(f"未知的场景：{scenario}") from exc


def render(template: str, values: Mapping[str, str]) -> str:
    """Substitute the {name} placeholders.

    Plain replacement rather than str.format for two reasons: the prompts
    contain a literal JSON example and format would read its braces as
    fields, and a user's typo'd {plcaeholder} has to survive as visible text
    instead of raising — a broken template is a normal event on a free-text
    field, not a 500.
    """
    for name, value in values.items():
        template = template.replace("{" + name + "}", value)
    return template
