"""The batch pass: a window of messages in, facts out.

Extraction is deferred so that the model sees a message in the company of
its neighbours — `хлеб 500` / `и молоко 300` is one shopping trip, and
one pass over the whole window converges on one `kind` where N separate
calls coin N synonyms for it.
"""

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from telegrind import llm, store, taxonomy
from telegrind.coerce import to_instant, to_json_value
from telegrind.config import ChatConfig
from telegrind.models import Chat, LoggedMessage

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


@dataclass(frozen=True, slots=True)
class Draft:
    """One fact the model returned, coerced but not yet stored."""

    message: LoggedMessage
    seq: int
    kind: str
    at: datetime
    fields: dict[str, object]


def drafts_from(
    payload: dict, tail: list[LoggedMessage], cfg: ChatConfig
) -> tuple[list[Draft], list[str]]:
    """Coerce the model's array. Returns (drafts, complaints).

    A complaint is something that could not be placed. It is returned
    rather than logged and forgotten, because a silently dropped fact is
    invisible in testing and the user is never told.
    """
    drafts: list[Draft] = []
    complaints: list[str] = []
    seen: dict[int, int] = {}

    for item in payload.get("facts") or []:
        index = item.get("message")
        if not isinstance(index, int) or not 1 <= index <= len(tail):
            complaints.append(f"факт указывает на сообщение {index!r} вне окна")
            continue

        kind = str(item.get("kind") or "").strip()
        if not kind:
            complaints.append(f"факт без kind в сообщении {index}")
            continue

        row = tail[index - 1]
        raw_fields = item.get("fields")
        fields = {
            str(key): to_json_value(value) for key, value in (raw_fields or {}).items()
        }
        # The model copies the phrase; the clock arithmetic is ours, against
        # the message's own timestamp rather than the moment of the pass.
        at = to_instant(item.get("when"), cfg, row.tg_date)

        seen[index] = seen.get(index, 0) + 1
        drafts.append(
            Draft(message=row, seq=seen[index], kind=kind, at=at, fields=fields)
        )

    return drafts, complaints


@dataclass(frozen=True, slots=True)
class Report:
    """What one pass did, in the shape /q reports it."""

    pending: int
    extracted: int
    facts: int
    failed: int
    complaints: int


async def run(
    session: AsyncSession,
    chat: Chat,
    cfg: ChatConfig,
    *,
    limit: int = 200,
    context_size: int = 10,
    call: Callable[..., Awaitable[dict]] = llm.use_tool,
) -> Report:
    """One extraction pass over the unextracted tail.

    The caller owns the transaction. Nothing here commits: /q wants the
    facts and the marks to land together or not at all.
    """
    tail = await store.unextracted_tail(session, chat.id, limit=limit)
    if not tail:
        return Report(pending=0, extracted=0, facts=0, failed=0, complaints=0)

    context = await store.context_before(session, chat.id, tail[0], limit=context_size)
    vocabulary = taxonomy.render(await taxonomy.observed(session, chat.id))
    prompt = build_prompt(tail, context, vocabulary, cfg, chat.chat_id)
    model = llm.current_model()

    try:
        payload = await call(llm.EXTRACT_SYSTEM, prompt, llm.EXTRACT_TOOL, model=model)
    except Exception as exc:
        # Not marked: the tail stays pending and the next /q retries it.
        store.mark_failed(tail, f"{type(exc).__name__}: {exc}")
        log.warning("extraction pass failed for chat %s: %s", chat.chat_id, exc)
        return Report(
            pending=len(tail), extracted=0, facts=0, failed=len(tail), complaints=0
        )

    drafts, complaints = drafts_from(payload, tail, cfg)
    for complaint in complaints:
        log.warning("extraction complaint in chat %s: %s", chat.chat_id, complaint)

    now = datetime.now(UTC)
    written = 0
    for row in tail:
        written += await store.replace_facts(
            session,
            chat_pk=chat.id,
            message_pk=row.id,
            drafts=[d for d in drafts if d.message is row],
            model=model,
            prompt_version=llm.PROMPT_VERSION,
            now=now,
        )
    store.mark_extracted(tail, model=model, prompt_version=llm.PROMPT_VERSION, at=now)

    return Report(
        pending=len(tail),
        extracted=len(tail),
        facts=written,
        failed=0,
        complaints=len(complaints),
    )
