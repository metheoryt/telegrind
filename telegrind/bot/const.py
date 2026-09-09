#: The service account the bot authenticates as. It has no Drive of its own
#: (storage quota 0), so it can never create or own a spreadsheet — only
#: read and write ones shared with it. That is why /link asks the user to
#: create the workbook, and why the user stays its owner.
SERVICE_ACCOUNT_EMAIL = "telegrind-bot@telegrind.iam.gserviceaccount.com"

INTRO_TEXT = """\
Привет! Пишите мне что угодно своими словами — я разберу и запишу.

Таблица не нужна: я всё храню у себя и ничего не теряю. \
Захотите видеть свои данные в Google Sheets — пришлите /link, \
и я подскажу, что сделать."""

TIP_TEXT = """<b>Пишите своими словами 📝</b>
---------------
<pre>4500 такси</pre>
<pre>5.4 usd хостинг</pre>
<pre>41 бат массаж вчера вечером</pre>
<pre>дал брату 20000 до конца месяца</pre>
<pre>вес 82.4</pre>
<pre>хочу велосипед gravel тысяч за 400</pre>
<pre>позвонил маме, всё хорошо</pre>

Ключевые слова не нужны — я читаю фразу целиком. \
В одном сообщении может быть несколько фактов, \
и ни один не теряется: то, что я не смогла отнести к категории, \
я сохраняю как есть.

<b>Изменить или удалить запись</b>
---------------
Чтобы <i>изменить</i> запись, <i>отредактируйте</i> своё сообщение.
Чтобы <i>удалить</i> запись, ответьте на неё знаком минуса "-".

<b>Таблица</b>
---------------
/link — подключить Google-таблицу или показать текущую
/unlink — отключить таблицу (записи останутся у меня)
/import — забрать в базу строки, которые уже есть в листах
/rebuild — перезаписать таблицу из моей базы
/reload — перечитать <code>_categories</code> и <code>_config</code>
"""
