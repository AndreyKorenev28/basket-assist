import shutil

from basket_assist.__main__ import main
from tests.conftest import EXAMPLE_TENANT


def test_check_loads_example_config(tmp_path, monkeypatch, capsys):
    cfg = tmp_path / "tenant.toml"
    shutil.copy(EXAMPLE_TENANT, cfg)
    monkeypatch.chdir(tmp_path)  # чтобы не подхватить .env проекта
    monkeypatch.setenv("DB_PATH", str(tmp_path / "data" / "app.db"))
    monkeypatch.setenv("TENANT_CONFIG", str(cfg))

    assert main(["--check"]) == 0
    assert "выступлений — 2" in capsys.readouterr().out
    assert (tmp_path / "data" / "app.db").exists()


def test_check_without_tenant_config(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TENANT_CONFIG", str(tmp_path / "missing.toml"))
    assert main(["--check"]) == 1
    assert "Нет файла конфигурации" in capsys.readouterr().out
