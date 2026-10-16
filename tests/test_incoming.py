"""Lifting an aiogram message into the log's columns (SPEC REQ-MSG-1, REQ-MSG-4, REQ-LOG-4, REQ-LOG-5)."""

from datetime import UTC, datetime

import pytest
from tgkit import NOW, bot_message, user_message

from telegrind.incoming import KINDS, incoming


def test_text_message():
    """REQ-MSG-1, REQ-LOG-5: kind text, text lifted, raw is model_dump(mode="json")."""
    inc = incoming(user_message(text="4500 такси"), now=1_760_000_000)
    assert inc.kind == "text" and inc.text == "4500 такси" and inc.author == "me"
    assert inc.occurred_at == int(NOW.timestamp()) and inc.received_at == 1_760_000_000
    assert inc.raw["text"] == "4500 такси" and inc.raw["chat"]["id"] == 7
    assert inc.media is None and inc.voice is None and inc.reply_to is None and inc.edited_at is None


@pytest.mark.parametrize(
    "kw, kind",
    [
        ({"photo": True, "caption": "чек"}, "photo"),
        ({"document": ("d9", "kaspi.pdf", 30_000)}, "document"),
        ({"voice": 15}, "voice"),
        ({"video_note": True}, "video_note"),
        ({"sticker": True}, "sticker"),
    ],
)
def test_kinds(kw, kind):
    """REQ-MSG-1: kind names the shape; text is the caption when there is one."""
    inc = incoming(user_message(**kw))
    assert inc.kind == kind and inc.kind in KINDS
    assert inc.text == kw.get("caption")


def test_photo_takes_the_largest_size_and_document_its_name():
    """REQ-MSG-2: the download target is the biggest photo; a document keeps its name, mime and size."""
    photo = incoming(user_message(photo=True)).media
    assert photo.file_id == "p-big" and photo.size == 250_000 and photo.mime == "image/jpeg"
    assert photo.name == "photo.jpg"
    doc = incoming(user_message(document=("d9", "kaspi.pdf", 30_000))).media
    assert (doc.file_id, doc.name, doc.mime, doc.size) == ("d9", "kaspi.pdf", "application/pdf", 30_000)


def test_voice_and_video_note_carry_duration_and_filename():
    """REQ-VOICE-1, REQ-VOICE-2: Scribe guesses the format from the filename; duration gates 20 minutes."""
    v = incoming(user_message(voice=15)).voice
    assert (v.file_id, v.duration, v.filename) == ("v1", 15, "voice.ogg")
    vn = incoming(user_message(video_note=True)).voice
    assert (vn.file_id, vn.duration, vn.filename) == ("vn1", 12, "video_note.mp4")


@pytest.mark.parametrize(
    "forward, author",
    [
        (("user", "Vasya Pupkin"), "forward: Vasya Pupkin"),
        (("hidden", "Vasya"), "forward: hidden Vasya"),
        (("chat", "Our chat"), "forward: Our chat"),
        (("channel", "News"), "forward: channel News"),
    ],
)
def test_forward_author_and_origin_date(forward, author):
    """REQ-MSG-4, REQ-LOG-4: who it was forwarded from, dated by the origin (a week earlier here)."""
    inc = incoming(user_message(text="x", forward=forward))
    assert inc.author == author
    assert inc.occurred_at == int(NOW.timestamp()) - 7 * 86400


def test_reply_and_edit_date():
    """REQ-LOG-4: reply_to is the replied message id; edit_date (a raw int in aiogram) is lifted as is."""
    inc = incoming(user_message(text="y", reply_to=bot_message(message_id=500), edit_date=1_760_000_123))
    assert inc.reply_to == 500 and inc.edited_at == 1_760_000_123


def test_other_kind_for_a_location():
    """REQ-MSG-1: anything else is `other`, stored all the same."""
    from aiogram.types import Chat, Location, Message, User

    msg = Message(
        message_id=1,
        date=datetime.now(UTC),
        chat=Chat(id=7, type="private"),
        from_user=User(id=7, is_bot=False, first_name="M"),
        location=Location(latitude=43.2, longitude=76.9),
    )
    assert incoming(msg).kind == "other" and incoming(msg).raw["location"]["latitude"] == 43.2
