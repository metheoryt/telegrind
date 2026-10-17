"""aiogram wiring: the three update types, each reaching the App (SPEC REQ-REACT-4)."""

from tgkit import FakeBot, reaction, user_message

from telegrind.app import App
from telegrind.bot import build_dispatcher
from telegrind.config import Config
from telegrind.layout import Layout


def test_reaction_handler_subscribes_message_reaction(tmp_path):
    """REQ-REACT-4: the dispatcher's resolved update types include message_reaction, message and edited_message."""
    cfg = Config.from_env({"BOT_TOKEN": "t", "ALLOWED_USER_IDS": "7", "DATA_DIR": str(tmp_path)})
    dp = build_dispatcher(App(config=cfg, bot=FakeBot(), layout=Layout(cfg.data_dir)))
    assert {"message", "edited_message", "message_reaction"} <= set(dp.resolve_used_update_types())


async def test_updates_reach_the_app(tmp_path):
    """REQ-MSG-1, REQ-MSG-3, REQ-REACT-1: a message, an edit and a reaction fed through the dispatcher are logged."""
    from aiogram.types import Update

    cfg = Config.from_env({"BOT_TOKEN": "t", "ALLOWED_USER_IDS": "7", "DATA_DIR": str(tmp_path)})
    app = App(config=cfg, bot=FakeBot(), layout=Layout(cfg.data_dir), clock=lambda: 1)
    dp = build_dispatcher(app)
    await dp.feed_update(app.bot, Update(update_id=1, message=user_message(text="x")))
    await dp.feed_update(app.bot, Update(update_id=2, edited_message=user_message(text="y", edit_date=2)))
    await dp.feed_update(app.bot, Update(update_id=3, message_reaction=reaction(emoji="💔")))
    entry = app.log_for(7).entry(10)
    assert entry.text == "y" and entry.deleted_at == 1 and entry.revision == 3
    app.close()
