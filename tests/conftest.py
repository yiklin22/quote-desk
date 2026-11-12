import json
from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from quotedesk import db
from quotedesk.domain import PriceList
from quotedesk.models import Base

ROOT = Path(__file__).parent.parent
FIXTURES = json.loads((ROOT / "fixtures" / "enquiries.json").read_text())
ENQUIRIES = FIXTURES["enquiries"]
TODAY = date.fromisoformat(FIXTURES["priced_on"])
BY_ID = {e["id"]: e for e in ENQUIRIES}


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
    yield s
    s.close()
