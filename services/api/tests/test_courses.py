from __future__ import annotations


def test_list_sample_courses(client):
    r = client.get("/courses")
    assert r.status_code == 200
    ids = {c["id"] for c in r.json()}
    assert {"aegean_600", "rolex_middle_sea", "rorc_channel"} <= ids


def test_load_aegean_course(client):
    r = client.get("/courses/aegean_600")
    assert r.status_code == 200
    c = r.json()
    assert c["start"] and c["finish"]
    assert len(c["marks"]) >= 3


def test_unknown_course_404(client):
    assert client.get("/courses/does_not_exist").status_code == 404
