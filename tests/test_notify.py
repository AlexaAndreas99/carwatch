"""Email notifications after a collection: what gets reported, and when."""

import smtplib
import textwrap

import pytest
from fastapi.testclient import TestClient

from carwatch import notify
from carwatch.adapters.base import RawListing
from carwatch.collector.engine import collect
from carwatch.config import load_config
from carwatch.db import init_db, session_scope
from carwatch.models import EventType, Favorite
from carwatch.web.app import create_app

CONFIG = """
settings:
  db_path: "{db}"
searches:
  - name: "Qashqai"
    sources:
      - site: "autovit"
        url: "https://www.autovit.ro/autoturisme/nissan/qashqai"
      - site: "olx"
        url: "https://www.olx.ro/autoturisme/nissan/qashqai/"
  - name: "Juke"
    sources:
      - site: "autovit"
        url: "https://www.autovit.ro/autoturisme/nissan/juke"
"""


def ad(ad_id, price, **kw):
    kw.setdefault("title", f"Nissan {ad_id}")
    kw.setdefault("url", f"https://www.autovit.ro/anunt/nissan-ID{ad_id}.html")
    kw.setdefault("mileage_km", 40000)
    kw.setdefault("year", 2024)
    return RawListing(site_listing_id=ad_id, price=price, currency="EUR", **kw)


class Adapter:
    """Serves scripted results per (search name, site)."""

    last_relaxation = None

    def __init__(self, results):
        self.results = results
        self.site = None

    def __call__(self, site, settings):
        self.site = site
        return self

    def fetch_listings(self, url, max_pages=None):
        for (name, site), rows in self.results.items():
            if site == self.site and name.lower() in url.lower():
                return rows
        return []


@pytest.fixture
def project(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    path.write_text(
        textwrap.dedent(CONFIG).format(db=(tmp_path / "t.db").as_posix()), encoding="utf-8"
    )
    sent = []
    # The feature is hidden in the app for now; these tests exercise it as built.
    monkeypatch.setattr(notify, "NOTIFICATIONS_VISIBLE", True)
    monkeypatch.setattr(notify, "send", lambda settings, message, password: sent.append(message))
    monkeypatch.setattr(notify, "get_password", lambda username: "secret")

    class Project:
        config_path = path
        outbox = sent

        def run(self, results):
            config = load_config(path)
            collect(config, adapter_factory=Adapter(results))
            self.db = config.db_file
            return self

        def configure(self, **changes):
            init_db(load_config(path).db_file)
            with session_scope(load_config(path).db_file) as session:
                settings = notify.NotifySettings(
                    enabled=True, smtp_host="smtp.example.com", username="me@example.com",
                    recipient="me@example.com",
                )
                for key, value in changes.items():
                    setattr(settings, key, value)
                notify.save_settings(session, settings)
            return self

        def star(self, site, ad_id):
            with session_scope(load_config(path).db_file) as session:
                session.add(Favorite(site=site, site_listing_id=ad_id, title="", url=""))
            return self

        def client(self):
            return TestClient(create_app(path))

    return Project()



def test_off_by_default_nothing_is_sent(project):
    project.run({("qashqai", "autovit"): [ad("1", 30000.0)]})
    project.run({("qashqai", "autovit"): [ad("1", 28000.0)]})

    assert project.outbox == []


def test_a_price_drop_over_the_minimum_is_emailed(project):
    project.run({("qashqai", "autovit"): [ad("1", 30000.0), ad("2", 25000.0)]})
    project.configure()

    project.run({("qashqai", "autovit"): [ad("1", 29000.0), ad("2", 24800.0)]})

    [message] = project.outbox
    assert message["Subject"] == "CarWatch: 1 price drop"
    text = message.get_body(("plain",)).get_content()
    assert "Nissan 1" in text
    assert "−1 000 (-3.3%): 30 000 → 29 000 EUR" in text
    assert "Nissan 2" not in text  # 200 EUR: under the 500 minimum


def test_the_first_collection_of_a_search_is_not_news(project):
    """Every car on it is "new" - an email listing them all would be noise."""
    project.configure(events=["new", "price_down"])

    project.run({("qashqai", "autovit"): [ad("1", 30000.0), ad("2", 25000.0)]})

    assert project.outbox == []


def test_new_listings_after_the_first_collection_are_reported_when_asked(project):
    project.run({("qashqai", "autovit"): [ad("1", 30000.0)]})
    project.configure(events=["new"])

    project.run({("qashqai", "autovit"): [ad("1", 30000.0), ad("2", 25000.0)]})

    [message] = project.outbox
    assert message["Subject"] == "CarWatch: 1 new listing"
    assert "new at 25 000 EUR" in message.get_body(("plain",)).get_content()


def test_only_favorites_leaves_out_everything_else(project):
    project.run({("qashqai", "autovit"): [ad("1", 30000.0), ad("2", 25000.0)]})
    project.star("autovit", "2").configure(scope="favorites")

    project.run({("qashqai", "autovit"): [ad("1", 28000.0), ad("2", 24000.0)]})

    [message] = project.outbox
    assert message["Subject"] == "CarWatch: 1 price drop (1 favorite)"
    text = message.get_body(("plain",)).get_content()
    assert "★ Nissan 2" in text and "Nissan 1" not in text


def test_favorites_come_first_when_every_car_is_reported(project):
    project.run({("qashqai", "autovit"): [ad("1", 30000.0), ad("2", 25000.0)]})
    project.star("autovit", "2").configure()

    project.run({("qashqai", "autovit"): [ad("1", 28000.0), ad("2", 24000.0)]})

    text = project.outbox[0].get_body(("plain",)).get_content()
    assert text.index("★ Nissan 2") < text.index("Nissan 1")


def test_a_muted_configuration_sends_nothing(project):
    results = {("qashqai", "autovit"): [ad("1", 30000.0)], ("juke", "autovit"): [ad("9", 20000.0)]}
    project.run(results)
    project.configure(muted=["Qashqai"])

    project.run({("qashqai", "autovit"): [ad("1", 28000.0)], ("juke", "autovit"): [ad("9", 20000.0)]})

    assert project.outbox == []


def test_a_car_on_two_sites_is_one_line(project):
    same = dict(url="https://www.autovit.ro/anunt/nissan-IDsame1.html", title="Nissan shared")
    project.run({("qashqai", "autovit"): [ad("same1", 30000.0, **same)],
                 ("qashqai", "olx"): [ad("777", 30000.0, **same)]})
    project.configure()

    project.run({("qashqai", "autovit"): [ad("same1", 29000.0, **same)],
                 ("qashqai", "olx"): [ad("777", 29000.0, **same)]})

    [message] = project.outbox
    assert message["Subject"] == "CarWatch: 1 price drop"
    assert "AUTOVIT + OLX" in message.get_body(("plain",)).get_content()


def test_gone_cars_are_reported_with_their_last_price(project):
    project.run({("qashqai", "autovit"): [ad("1", 30000.0), ad("2", 25000.0)]})
    project.configure(events=["delisted"])

    project.run({("qashqai", "autovit"): [ad("1", 30000.0)]})

    [message] = project.outbox
    assert message["Subject"] == "CarWatch: 1 car gone"
    assert "gone, last asking 25 000 EUR" in message.get_body(("plain",)).get_content()


def test_a_failed_send_is_recorded_and_the_collection_still_stands(project, monkeypatch):
    def refuse(settings, message, password):
        raise smtplib.SMTPAuthenticationError(535, b"bad credentials")

    project.run({("qashqai", "autovit"): [ad("1", 30000.0)]})
    project.configure()
    monkeypatch.setattr(notify, "send", refuse)

    project.run({("qashqai", "autovit"): [ad("1", 28000.0)]})

    with session_scope(project.db) as session:
        last = notify.load_last(session)
    assert last.ok is False and last.changes == 1
    assert "refused the login" in last.error


def test_the_html_part_carries_every_change(project):
    project.run({("qashqai", "autovit"): [ad("1", 30000.0)]})
    project.configure()
    project.run({("qashqai", "autovit"): [ad("1", 28000.0)]})

    html = project.outbox[0].get_body(("html",)).get_content()
    assert "Nissan 1" in html and "30 000 → 28 000 EUR" in html


# ------------------------------------------------------------- dashboard


def form(**overrides):
    data = {
        "enabled": "on", "scope": "all", "events": ["price_down"], "min_change": "500",
        "configs": ["Qashqai", "Juke"], "smtp_host": "smtp.gmail.com", "smtp_port": "587",
        "security": "starttls", "username": "me@gmail.com", "password": "",
        "sender": "", "recipient": "me@gmail.com",
    }
    data.update(overrides)
    return data


def test_the_runs_page_loads_the_panel_after_itself(project):
    project.run({})
    client = project.client()

    assert 'hx-get="/notifications"' in client.get("/runs").text
    assert "Email notifications" in client.get("/notifications").text


def test_saving_the_panel_stores_settings_and_keeps_the_password_out_of_them(project, monkeypatch):
    stored = {}
    monkeypatch.setattr(notify, "set_password", lambda user, pw: stored.update({user: pw}))
    project.run({})

    body = project.client().post("/notifications", data=form(password="app-pass", configs=["Juke"])).text

    assert "Saved." in body
    assert stored == {"me@gmail.com": "app-pass"}
    with session_scope(project.db) as session:
        settings = notify.load_settings(session)
        raw = session.get(notify.AppState, notify.SETTINGS_KEY).value
    assert settings.enabled and settings.muted == ["Qashqai"]
    assert "app-pass" not in raw


def test_switching_on_without_a_server_is_refused(project):
    project.run({})

    body = project.client().post("/notifications", data=form(smtp_host="")).text

    assert "Not done." in body


def test_switching_on_with_no_kind_of_change_is_refused(project):
    project.run({})

    body = project.client().post("/notifications", data=form(events=[])).text

    assert "Pick at least one kind of change" in body


def test_the_test_button_sends_a_sample(project):
    project.run({})
    project.configure()

    body = project.client().post("/notifications/test").text

    assert "Test email sent to me@example.com" in body
    assert project.outbox[0]["Subject"] == "CarWatch: test email — notifications are set up"


def test_event_labels_cover_every_type():
    assert set(notify.EVENT_LABELS) == set(EventType)


# ------------------------------------------------------------ hidden


def test_hidden_there_is_no_panel_and_no_email(project, monkeypatch):
    project.run({("qashqai", "autovit"): [ad("1", 30000.0)]})
    project.configure()  # settings saved while it was visible
    monkeypatch.setattr(notify, "NOTIFICATIONS_VISIBLE", False)

    project.run({("qashqai", "autovit"): [ad("1", 28000.0)]})
    client = project.client()

    assert project.outbox == []
    assert 'hx-get="/notifications"' not in client.get("/runs").text
    assert client.get("/notifications").status_code == 404
    assert client.post("/notifications", data=form()).status_code == 404
    assert client.post("/notifications/test").status_code == 404


def test_the_feature_ships_hidden():
    """Guards the switch itself; the fixture above flips it only for its tests."""
    assert notify.NOTIFICATIONS_VISIBLE is False
