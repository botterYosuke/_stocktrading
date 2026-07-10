from __future__ import annotations

from pathlib import Path

from stocktrading.config import load_settings


def test_load_settings_from_env_file(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                r"BACKCAST_ROOT=C:\Users\sasai\Documents\backcast",
                r"BOARD_SOURCE_ROOT=S:\jp\stocks_board_kabu_push",
                r"MEDALLION_ROOT=.\data",
            ]
        ),
        encoding="utf-8",
    )

    settings = load_settings(env_file)

    assert settings.backcast_root.name == "backcast"
    assert settings.board_source_root == Path(r"S:\jp\stocks_board_kabu_push")
    assert settings.bronze_root == Path(r".\data") / "bronze"
