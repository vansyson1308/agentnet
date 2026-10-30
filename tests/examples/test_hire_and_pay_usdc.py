import importlib.util
import os
from types import SimpleNamespace

import pytest

_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "examples", "hire_and_pay_usdc.py")
_spec = importlib.util.spec_from_file_location("hire_and_pay_usdc", _PATH)
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)

KEY = "sk-test-secret-key-123"


class FakeClient:
    def __init__(self, price=1.0, statuses=("pending", "completed")):
        self.price = price
        self.statuses = list(statuses)
        self.create_calls = 0

    def search_agents(self, capability=None, max_price=None):
        return [SimpleNamespace(id="a1", name="Bot", capabilities=[{"name": capability, "price": self.price}])]

    def create_task(self, capability=None, agent_id=None, escrow_amount=None, currency=None):
        self.create_calls += 1
        return SimpleNamespace(id="t1")

    def get_task(self, task_id):
        status = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
        return {"id": task_id, "status": status, "result": "done-output"}


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("AGENTNET_API_KEY", KEY)


def run(fake, extra=()):
    argv = ["--capability", "summarize", "--max-spend", "2.0", "--poll-interval", "0", *extra]
    return mod.main(argv, client_factory=lambda url, key: fake, sleep=lambda s: None)


def test_happy_path(capsys):
    fake = FakeClient()
    assert run(fake) == 0
    out = capsys.readouterr()
    assert "price: 1.0" in out.out
    assert "done-output" in out.out
    assert KEY not in out.out + out.err


def test_max_spend_abort(capsys):
    fake = FakeClient(price=5.0)
    assert run(fake) != 0
    assert fake.create_calls == 0
    out = capsys.readouterr()
    assert KEY not in out.out + out.err


def test_single_create_task():
    fake = FakeClient(statuses=("pending", "pending", "completed"))
    assert run(fake) == 0
    assert fake.create_calls == 1


def test_production_refused(capsys):
    fake = FakeClient()
    assert run(fake, ["--base-url", "https://api.agentnet.dev"]) != 0
    assert fake.create_calls == 0
    assert run(fake, ["--base-url", "https://api.agentnet.dev", "--allow-production"]) == 0


def test_non_https_refused():
    fake = FakeClient()
    assert run(fake, ["--base-url", "http://example.com"]) != 0
    assert fake.create_calls == 0


def test_key_not_leaked_on_error(capsys):
    class Boom(FakeClient):
        def search_agents(self, **kw):
            raise RuntimeError("failed with key " + KEY)

    assert run(Boom()) != 0
    out = capsys.readouterr()
    assert KEY not in out.out + out.err
