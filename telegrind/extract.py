"""The batch pass: a window of messages in, facts out.

Extraction is deferred so that the model sees a message in the company of
its neighbours — `хлеб 500` / `и молоко 300` is one shopping trip, and
one pass over the whole window converges on one `kind` where N separate
calls coin N synonyms for it.
"""

import logging

from telegrind.config import ChatConfig
from telegrind.models import LoggedMessage

log = logging.getLogger(__name__)


def author_of(row: LoggedMessage, chat_id: int) -> str:
    """Who wrote this, as the prompt states it.

    Three arms, not two. `MessageOriginHiddenUser` carries a display name
    and no id, so "is this me?" has no answer — and every rule that
    branches on authorship dies on that `None` if the third arm is
    missing. Nothing is dropped for where it came from: authorship
    changes what a fact *means*, and meaning is the model's job.
    """
    origin = (row.raw or {}).get("forward_origin")
    if not origin:
        return "я"

    kind = origin.get("type")
    if kind == "user":
        sender = origin.get("sender_user") or {}
        if sender.get("id") == chat_id:
            return "я"
        name = sender.get("first_name") or sender.get("username") or "кто-то"
        return f"переслано от «{name}»"
    if kind == "hidden_user":
        name = origin.get("sender_user_name") or "кто-то"
        return f"переслано от «{name}» (кто именно — неизвестно)"

    title = (origin.get("chat") or {}).get("title") or "без названия"
    where = "канала" if kind == "channel" else "чата"
    return f"переслано из {where} «{title}»"


def _reply_to(row: LoggedMessage) -> int | None:
    """The Telegram message_id this one replies to, if any."""
    return ((row.raw or {}).get("reply_to_message") or {}).get("message_id")


def _line(
    marker: str,
    row: LoggedMessage,
    cfg: ChatConfig,
    chat_id: int,
    markers: dict[int, str],
) -> str:
    stamp = cfg.localized(row.tg_date).strftime("%Y-%m-%d %H:%M")
    head = f"[{marker}] {stamp} ({author_of(row, chat_id)})"
    parent = _reply_to(row)
    if parent is not None:
        seen = markers.get(parent)
        head += f" → ответ на [{seen}]" if seen else " → ответ на сообщение вне окна"
    return f"{head}: {row.content}"


def build_prompt(
    tail: list[LoggedMessage],
    context: list[LoggedMessage],
    taxonomy: str,
    cfg: ChatConfig,
    chat_id: int,
) -> str:
    """The user turn: the taxonomy, the read-only context, the tail."""
    markers: dict[int, str] = {
        row.message_id: f"C{i}" for i, row in enumerate(context, 1)
    }
    markers |= {row.message_id: str(i) for i, row in enumerate(tail, 1)}

    blocks = [
        "# Словарь этого чата",
        "Переиспользуй существующий kind и существующие имена полей, если "
        "подходят. Заводи новые, только если ничего не подходит.",
        taxonomy,
        "",
        f"# Валюта по умолчанию\n{cfg.currency}",
        "",
    ]
    if context:
        blocks += [
            "# Контекст (уже разобран, извлекать из него НЕ надо)",
            "\n".join(
                _line(f"C{i}", row, cfg, chat_id, markers)
                for i, row in enumerate(context, 1)
            ),
            "",
        ]
    blocks += [
        "# Сообщения для разбора",
        "Сообщение, помеченное «ответ на [X]», продолжает сообщение X: читай "
        "их вместе. Если оба здесь и описывают одно и то же — заведи один "
        "факт, а не два.",
        "\n".join(
            _line(str(i), row, cfg, chat_id, markers) for i, row in enumerate(tail, 1)
        ),
    ]
    return "\n".join(blocks)
