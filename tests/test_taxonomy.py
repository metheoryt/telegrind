from telegrind.taxonomy import KindUsage, observed, render


class FakeResult:
    def __init__(self, rows: list[tuple]) -> None:
        self._rows = rows

    def all(self) -> list[tuple]:
        return self._rows


class FakeSession:
    def __init__(self, rows: list[tuple]) -> None:
        self._rows = rows
        self.statements: list[object] = []

    async def execute(self, statement: object) -> FakeResult:
        self.statements.append(statement)
        return FakeResult(self._rows)


async def test_observed_groups_fields_under_their_kind() -> None:
    session = FakeSession(
        [
            ("expense", "amount", 128),
            ("expense", "comment", 128),
            ("expense", "currency", 120),
            ("loan", "amount", 31),
            ("loan", "counterparty", 31),
        ]
    )

    usages = await observed(session, chat_pk=1)

    assert usages == [
        KindUsage(kind="expense", fields=("amount", "comment", "currency"), count=128),
        KindUsage(kind="loan", fields=("amount", "counterparty"), count=31),
    ]


async def test_observed_orders_kinds_by_how_often_they_occur() -> None:
    session = FakeSession([("wish", "text", 3), ("expense", "amount", 128)])

    usages = await observed(session, chat_pk=1)

    assert [u.kind for u in usages] == ["expense", "wish"]


async def test_render_is_one_line_per_kind() -> None:
    rendered = render(
        [KindUsage(kind="expense", fields=("amount", "comment"), count=128)]
    )

    assert rendered == "- expense (128): amount, comment"


async def test_render_says_so_when_nothing_has_been_observed_yet() -> None:
    assert "пока" in render([])
