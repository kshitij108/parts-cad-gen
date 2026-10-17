"""End-to-end through the web app: upload, answer questions, review, approve, delete."""

import re

import pytest
from fastapi.testclient import TestClient

from app.db import Job
from app.main import create_app


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("PARTS_CAD_DATA", str(tmp_path / "data"))
    app = create_app(f"sqlite:///{tmp_path}/test.db")
    with TestClient(app) as c:
        yield c


def _upload(client, step_dir, **form):
    with open(step_dir / "bracket.step", "rb") as f:
        r = client.post("/jobs", files=[("files", ("bracket.step", f.read(), "application/step"))],
                        data={"title": "Bracket", **form}, follow_redirects=False)
    assert r.status_code == 303, r.text
    return r.headers["location"].split("?")[0]


def _line_ids(html):
    return re.findall(r'<tr id="(q\d+\.[\w.\-]+)"', html)


def test_missing_info_blocks_then_review_and_approve(client, step_dir, tmp_path):
    url = _upload(client, step_dir)  # no material, no quantity
    page = client.get(url).text
    assert "needs info" in page and "Which material?" in page
    assert "Approve package" in page and "disabled" in page

    r = client.post(f"{url}/spec", data={"actor": "Sam", "material_id": "al6061", "quantities": "1, 25"}, follow_redirects=False)
    assert r.status_code == 303
    page = client.get(url).text
    assert "in review" in page
    lines = _line_ids(page)
    assert lines and any(l.startswith("q25.") for l in lines)

    # Approval refused while lines are undecided.
    r = client.post(f"{url}/approve", data={"actor": "Sam"}, follow_redirects=False)
    assert "error=" in r.headers["location"]

    # Edit without a note is refused.
    r = client.post(f"{url}/lines/{lines[0]}", data={"action": "edit", "actor": "Sam", "amount": "1"}, follow_redirects=False)
    assert "error=" in r.headers["location"]

    for i, lid in enumerate(lines):
        amount = re.search(rf'id="{re.escape(lid)}".*?name="amount"[^>]*value="([\d.]+)"', page, re.S).group(1)
        action, note = ("reject", "customer supplies stock") if lid.endswith(".material") and lid.startswith("q1.") else ("approve", "")
        r = client.post(f"{url}/lines/{lid}", data={"action": action, "actor": "Sam", "amount": amount, "note": note}, follow_redirects=False)
        assert "error=" not in r.headers["location"], r.headers["location"]

    r = client.post(f"{url}/approve", data={"actor": "Sam", "note": "ok"}, follow_redirects=False)
    assert "error=" not in r.headers["location"], r.headers["location"]
    pkg = client.get(f"{url}/package.json").json()
    assert pkg["approved_by"] == "Sam"
    assert pkg["originals"][0]["name"] == "bracket.step" and len(pkg["originals"][0]["sha256"]) == 64
    assert set(pkg["stages"]) == {"intake", "geometry", "drawing", "dfm", "quote"}
    assert pkg["decisions"]["q1.material"]["action"] == "reject"
    assert all(v is not None for v in pkg["totals"].values())

    # Frozen after approval.
    r = client.post(f"{url}/lines/{lines[1]}", data={"action": "approve", "actor": "Sam", "amount": "0"}, follow_redirects=False)
    assert "error=" in r.headers["location"]

    csv = client.get(f"{url}/quote.csv").text
    assert csv.startswith("quantity,line_id") and "q25.machining" in csv


def test_delete_removes_files(client, step_dir, tmp_path):
    url = _upload(client, step_dir, material_id="al6061", quantities="5")
    originals = list((tmp_path / "data" / "jobs").glob("*/originals/*"))
    assert len(originals) == 1
    r = client.post(f"{url}/delete", data={"confirm": "DELETE"}, follow_redirects=False)
    assert r.status_code == 303
    assert not list((tmp_path / "data" / "jobs").glob("*"))
    assert client.get(url).status_code == 404


def test_rate_card_validation(client):
    r = client.post("/rate-card", data={"raw": '{"shop_name": "x"}'}, follow_redirects=False)
    assert "error=" in r.headers["location"]
    page = client.get("/").text
    assert "example numbers" in page
