import pytest

from sarcompare.demo import demo_config


@pytest.fixture(scope="session")
def demo_cfg(tmp_path_factory):
    root = tmp_path_factory.mktemp("demo")
    return demo_config(root / "data", str(root / "out"))
