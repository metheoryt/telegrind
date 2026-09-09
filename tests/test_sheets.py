from telegrind.sheets import Config, data_range, parse_config


def test_parse_config_reads_both_keys() -> None:
    cfg = parse_config([["Часовой пояс (в часах)", "3"], ["Основная валюта", "usd"]])
    assert cfg.dt_offset == 3
    assert cfg.currency == "USD"


def test_parse_config_survives_a_blank_value_column() -> None:
    cfg = parse_config([["Часовой пояс (в часах)"], ["Основная валюта", ""]])
    assert cfg == Config()


def test_parse_config_survives_an_empty_sheet() -> None:
    assert parse_config([]) == Config()


def test_parse_config_survives_a_non_numeric_offset() -> None:
    cfg = parse_config(
        [["Часовой пояс (в часах)", "шесть"], ["Основная валюта", "KZT"]]
    )
    assert cfg.dt_offset == Config().dt_offset


def test_parse_config_survives_an_invalid_currency() -> None:
    cfg = parse_config([["Часовой пояс (в часах)", "6"], ["Основная валюта", "XXXXX"]])
    assert cfg.currency == Config().currency


def test_data_range_covers_the_declared_columns_only() -> None:
    assert data_range(5) == "A2:E"
    assert data_range(1) == "A2:A"


def test_data_range_past_column_z() -> None:
    assert data_range(27) == "A2:AA"
