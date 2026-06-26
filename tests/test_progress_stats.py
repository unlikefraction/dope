from __future__ import annotations

import importlib
from datetime import date, datetime, time, timedelta, timezone

from fastapi.testclient import TestClient


def load_main(tmp_path, monkeypatch):
    monkeypatch.setenv("DOPE_DB_PATH", str(tmp_path / "dope.db"))
    import app.main as main

    return importlib.reload(main)


def ist_time_to_utc_iso(main, day: date, hour: int, minute: int = 0) -> str:
    ist_wall_time = datetime.combine(day, time(hour, minute), tzinfo=timezone.utc)
    return (ist_wall_time - main.IST_OFFSET).isoformat(timespec="seconds")


def test_dope_day_resets_at_9am_ist(tmp_path, monkeypatch):
    main = load_main(tmp_path, monkeypatch)

    assert main.dope_day_for("2026-05-24T03:29:59+00:00") == date(2026, 5, 23)
    assert main.dope_day_for("2026-05-24T03:30:00+00:00") == date(2026, 5, 24)


def test_index_supports_head_for_deploy_checks(tmp_path, monkeypatch):
    main = load_main(tmp_path, monkeypatch)

    with TestClient(main.app) as client:
        response = client.head("/")

    assert response.status_code == 200


def test_progress_stats_groups_completed_hours_by_person(tmp_path, monkeypatch):
    main = load_main(tmp_path, monkeypatch)

    with TestClient(main.app) as client:
        client.post(
            "/api/auth/signup",
            json={"username": "shubham", "password": "password", "display_name": "Shubham"},
        )
        client.post(
            "/api/auth/signup",
            json={"username": "saket", "password": "password", "display_name": "Saket"},
        )
        login = client.post("/api/auth/login", json={"username": "shubham", "password": "password"})
        assert login.status_code == 200

        today = main.current_dope_day()
        yesterday = today - timedelta(days=1)
        with main.db() as conn:
            users = {
                row["username"]: row["id"]
                for row in conn.execute("SELECT id, username FROM users").fetchall()
            }
            rows = [
                ("A", 120, users["shubham"], ist_time_to_utc_iso(main, today, 10)),
                ("B", 30, users["shubham"], ist_time_to_utc_iso(main, today, 18)),
                ("C", 60, users["saket"], ist_time_to_utc_iso(main, today, 11)),
                ("D", 45, users["saket"], ist_time_to_utc_iso(main, yesterday, 9)),
            ]
            conn.executemany(
                """
                INSERT INTO dopes
                  (title, description_html, time_minutes, created_by, created_at, completed_by, completed_at)
                VALUES (?, '<p>Done</p>', ?, ?, ?, ?, ?)
                """,
                [(title, minutes, user_id, completed_at, user_id, completed_at) for title, minutes, user_id, completed_at in rows],
            )

        response = client.get("/api/stats/progress?days=7")
        assert response.status_code == 200
        payload = response.json()

    today_bucket = next(day for day in payload if day["date"] == today.isoformat())
    yesterday_bucket = next(day for day in payload if day["date"] == yesterday.isoformat())

    assert today_bucket["total_minutes"] == 210
    assert today_bucket["stacks"] == [
        {"user_id": users["shubham"], "display_name": "Shubham", "color": "#1a1a1a", "minutes": 150, "count": 2},
        {"user_id": users["saket"], "display_name": "Saket", "color": "#1a1a1a", "minutes": 60, "count": 1},
    ]
    assert yesterday_bucket["total_minutes"] == 45
    assert yesterday_bucket["stacks"][0]["display_name"] == "Saket"


def test_profile_color_is_used_in_progress_stats(tmp_path, monkeypatch):
    main = load_main(tmp_path, monkeypatch)

    with TestClient(main.app) as client:
        client.post(
            "/api/auth/signup",
            json={"username": "shubham", "password": "password", "display_name": "Shubham"},
        )
        client.post("/api/auth/login", json={"username": "shubham", "password": "password"})
        response = client.patch("/api/me", json={"display_name": "Shubham S", "color": "#ffbb00"})
        assert response.status_code == 200
        assert response.json()["color"] == "#ffbb00"

        today = main.current_dope_day()
        completed_at = ist_time_to_utc_iso(main, today, 10)
        with main.db() as conn:
            user_id = conn.execute("SELECT id FROM users WHERE username = 'shubham'").fetchone()[0]
            conn.execute(
                """
                INSERT INTO dopes
                  (title, description_html, time_minutes, created_by, created_at, completed_by, completed_at)
                VALUES ('A', '<p>Done</p>', 30, ?, ?, ?, ?)
                """,
                (user_id, completed_at, user_id, completed_at),
            )

        payload = client.get("/api/stats/progress?days=7").json()
    today_bucket = next(day for day in payload if day["date"] == today.isoformat())
    assert today_bucket["stacks"][0]["display_name"] == "Shubham S"
    assert today_bucket["stacks"][0]["color"] == "#ffbb00"


def test_dependent_count_includes_transitive_children(tmp_path, monkeypatch):
    main = load_main(tmp_path, monkeypatch)

    with TestClient(main.app) as client:
        client.post(
            "/api/auth/signup",
            json={"username": "shubham", "password": "password", "display_name": "Shubham"},
        )
        client.post("/api/auth/login", json={"username": "shubham", "password": "password"})
        with main.db() as conn:
            user_id = conn.execute("SELECT id FROM users WHERE username = 'shubham'").fetchone()[0]
            now = main.now_iso()
            conn.executemany(
                """
                INSERT INTO dopes (id, title, description_html, time_minutes, created_by, created_at)
                VALUES (?, ?, '<p>Do it</p>', 30, ?, ?)
                """,
                [(1, "C", user_id, now), (2, "B", user_id, now), (3, "A", user_id, now)],
            )
            conn.executemany(
                "INSERT INTO dope_dependencies (dope_id, depends_on_id, created_at) VALUES (?, ?, ?)",
                [(2, 1, now), (3, 2, now)],
            )

        payload = client.get("/api/dopes?status=active").json()
    by_title = {item["title"]: item for item in payload}
    assert by_title["C"]["dependent_count"] == 2
    assert [item["title"] for item in by_title["C"]["dependents"]] == ["B", "A"]
    assert [item["depth"] for item in by_title["C"]["dependents"]] == [1, 2]
    assert by_title["B"]["dependent_count"] == 1


def test_categories_seeded_and_manageable(tmp_path, monkeypatch):
    main = load_main(tmp_path, monkeypatch)

    with TestClient(main.app) as client:
        client.post(
            "/api/auth/signup",
            json={"username": "shubham", "password": "password", "display_name": "Shubham"},
        )
        client.post("/api/auth/login", json={"username": "shubham", "password": "password"})

        seeded = client.get("/api/categories").json()
        assert [c["name"] for c in seeded] == [
            "Silicon Centered",
            "Silicon Supporting",
            "Client Side",
            "Team",
        ]
        centered = seeded[0]
        assert centered["color"] == "#2e6f8e"

        created = client.post("/api/categories", json={"name": "Research", "color": "#b23a70"})
        assert created.status_code == 200
        new_id = created.json()["id"]

        # Duplicate names are rejected
        assert client.post("/api/categories", json={"name": "research", "color": "#000000"}).status_code == 409

        renamed = client.patch(f"/api/categories/{new_id}", json={"name": "Discovery", "color": "#4f68b1"})
        assert renamed.status_code == 200
        assert renamed.json()["name"] == "Discovery"

        # Create a dope in the category, then deleting the category should null it out
        dope = client.post(
            "/api/dopes",
            json={
                "title": "Categorized",
                "description_html": "<p>Body</p>",
                "time_text": "30min",
                "dependency_ids": [],
                "category_id": new_id,
            },
        ).json()
        assert dope["category"]["name"] == "Discovery"

        assert client.delete(f"/api/categories/{new_id}").status_code == 200
        refreshed = client.get("/api/dopes?status=active").json()
        assert refreshed[0]["category"] is None

        # Unknown category is rejected on create
        bad = client.post(
            "/api/dopes",
            json={
                "title": "Bad",
                "description_html": "<p>x</p>",
                "time_text": "30min",
                "dependency_ids": [],
                "category_id": 99999,
            },
        )
        assert bad.status_code == 400


def test_api_key_can_manage_dopes(tmp_path, monkeypatch):
    main = load_main(tmp_path, monkeypatch)

    with TestClient(main.app) as client:
        client.post(
            "/api/auth/signup",
            json={"username": "shubham", "password": "password", "display_name": "Shubham"},
        )
        client.post("/api/auth/login", json={"username": "shubham", "password": "password"})
        created = client.post("/api/me/keys", json={"name": "Codex"}).json()
        key = created["key"]
        headers = {"Authorization": f"Bearer {key}"}

        me = client.get("/api/me", headers=headers)
        assert me.status_code == 200
        assert me.json()["display_name"] == "Shubham"

        dope = client.post(
            "/api/dopes",
            headers=headers,
            json={"title": "API managed", "description_html": "<p>From API</p>", "time_text": "30min", "dependency_ids": []},
        )
        assert dope.status_code == 200
        dope_id = dope.json()["id"]

        assigned = client.post(f"/api/dopes/{dope_id}/assign", headers=headers)
        assert assigned.status_code == 200
        assert assigned.json()["assigned_to"]["display_name"] == "Shubham"

        completed = client.post(
            f"/api/dopes/{dope_id}/complete",
            headers=headers,
            json={"completion_text": "Done in API https://github.com/teamofsilicons/dope/commit/abc123"},
        )
        assert completed.status_code == 200
        assert completed.json()["status"] == "completed"

        active = client.get("/api/dopes?status=active", headers=headers)
        assert active.status_code == 200

        client.delete(f"/api/me/keys/{created['id']}")
        client.cookies.clear()
        unauthorized = client.get("/api/me", headers=headers)
        assert unauthorized.status_code == 401
