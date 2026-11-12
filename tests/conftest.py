import json
from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from quotedesk import auth, db
from quotedesk.domain import PriceList
from quotedesk.models import Account, Base

ROOT = Path(__file__).parent.parent
FIXTURES = json.loads((ROOT / "fixtures" / "enquiries.json").read_text())
ENQUIRIES = FIXTURES["enquiries"]
TODAY = date.fromisoformat(FIXTURES["priced_on"])
BY_ID = {e["id"]: e for e in ENQUIRIES}
ACC = 1                                  # the account every older test works in
EMAIL, PASSWORD = "ana@example.test", "correct horse battery"
PASSWORD_HASH = auth.hash_password(PASSWORD)     # hashed once; scrypt is slow on purpose


def add_account(session, email, name) -> Account:
    account = Account(email=email, name=name, password_hash=PASSWORD_HASH)
    session.add(account)
    session.commit()
    return account


def log_in(client, email=EMAIL, password=PASSWORD):
    r = client.post("/login", data={"email": email, "password": password}, follow_redirects=False)
    assert r.status_code == 303, r.text
    return client


@pytest.fixture(scope="session")
def price_list():
    return PriceList.model_validate_json((ROOT / "quotedesk" / "pricelist_v1.json").read_text())


@pytest.fixture(scope="session")
def engine(tmp_path_factory):
    engine = create_engine(db.start_embedded(tmp_path_factory.mktemp("pg")))
    db.set_engine(engine)
    return engine


@pytest.fixture
def session(engine):
    with engine.begin() as conn:
        conn.execute(text(f"TRUNCATE {', '.join(t.name for t in Base.metadata.sorted_tables)} RESTART IDENTITY CASCADE"))
    s = db.new_session()
    assert add_account(s, EMAIL, "Ana").id == ACC
    yield s
    s.close()


@pytest.fixture
def anon(session):
    """A browser with no login."""
    from fastapi.testclient import TestClient

    from quotedesk.app import app
    with TestClient(app) as c:
        yield c


@pytest.fixture
def client(anon):
    """A browser logged in as Ana, the owner of account 1."""
    return log_in(anon)
