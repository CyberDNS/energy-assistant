"""DeviceConfigStore: edit device entries of config.yaml, keep everything else."""

from __future__ import annotations

from pathlib import Path

import pytest

from nornwatt.config.store import (
    KEEP_BACKUPS,
    ConfigConflictError,
    ConfigDocument,
    ConfigStoreError,
    DeviceConfigStore,
    entry_to_form,
    form_to_entry,
)
from nornwatt.core.device_model import ParamSpec

TEXT = """\
backends:
    mqtt: { host: broker }   # odd indentation stays as it is

devices:
  # ── Cars ──
  - id: wallbox
    category: charger
    type: openwb_mqtt
    chargepoint_id: 5
    params:
      min_power_kw: 4.14   # 6 A on three phases
      default_vehicle: car_a
      guest_mode: pv_only

  # the family car
  - id: car_a
    category: vehicle
    type: homeassistant_vehicle
    capabilities:
      soc_pct: { entity: sensor.car_a_soc }
    params:
      capacity_kwh: 77
      charge_curve:
        - { soc_pct: 90, efficiency: 0.90 }   # full rate
        - { soc_pct: 100, efficiency: 0.55 }
    # refresh_entity: button.car_a_refresh

  - id: meter
    category: meter
    type: tibber_local
    host: 10.0.0.2
    password: !secret pulse_password

optimizer:
  horizon_hours: 24
"""


def _changed_lines(old: str, new: str) -> tuple[list[str], list[str]]:
    old_lines, new_lines = old.splitlines(), new.splitlines()
    return ([l for l in old_lines if l not in new_lines], [l for l in new_lines if l not in old_lines])


def test_entries_are_plain_data_with_secret_references() -> None:
    doc = ConfigDocument(TEXT)

    assert doc.device_ids() == ["wallbox", "car_a", "meter"]
    assert doc.entry("meter") == {
        "category": "meter", "type": "tibber_local", "host": "10.0.0.2",
        "password": {"!secret": "pulse_password"},
    }
    assert doc.entry("car_a")["params"]["charge_curve"][1] == {"soc_pct": 100, "efficiency": 0.55}
    assert doc.entry("nope") is None


def test_unchanged_entry_leaves_the_file_untouched() -> None:
    doc = ConfigDocument(TEXT)
    for device_id in doc.device_ids():
        assert doc.with_device(device_id, doc.entry(device_id)) == TEXT


def test_changing_a_value_changes_only_that_line() -> None:
    doc = ConfigDocument(TEXT)
    entry = doc.entry("wallbox")
    entry["params"]["default_vehicle"] = "car_b"

    new = doc.with_device("wallbox", entry)

    assert _changed_lines(TEXT, new) == (
        ["      default_vehicle: car_a"], ["      default_vehicle: car_b"],
    )


def test_adding_and_removing_keys_keeps_comments_and_the_other_lines() -> None:
    doc = ConfigDocument(TEXT)
    entry = doc.entry("car_a")
    entry["label"] = "Car A"
    entry["params"]["charge_limit_soc_pct"] = 90
    del entry["params"]["capacity_kwh"]

    new = doc.with_device("car_a", entry)

    removed, added = _changed_lines(TEXT, new)
    assert removed == ["      capacity_kwh: 77"]
    assert sorted(added) == ["      charge_limit_soc_pct: 90", "    label: Car A"]
    assert "        - { soc_pct: 90, efficiency: 0.90 }   # full rate\n" in new
    assert new.count("    # refresh_entity: button.car_a_refresh\n") == 1
    assert new.index("    label: Car A\n") < new.index("    # refresh_entity")
    assert ConfigDocument(new).entry("car_a") == entry


def test_secret_reference_survives_an_edit_and_can_be_set() -> None:
    doc = ConfigDocument(TEXT)
    entry = doc.entry("meter")
    entry["host"] = "10.0.0.3"
    entry["token"] = {"!secret": "tibber_token"}

    new = doc.with_device("meter", entry)

    assert "    password: !secret pulse_password\n" in new
    assert "    token: !secret tibber_token\n" in new
    assert "    host: 10.0.0.3\n" in new


def test_new_device_goes_after_the_last_one_of_its_category() -> None:
    doc = ConfigDocument(TEXT)

    new = doc.with_device("car_b", {
        "type": "homeassistant_vehicle", "category": "vehicle", "label": "Car B",
        "capabilities": {"soc_pct": {"entity": "sensor.car_b_soc"}},
        "params": {"capacity_kwh": 40.0, "previous_ids": ["ev_b"]},
    })

    assert new.replace("""\

  - id: car_b
    category: vehicle
    label: Car B
    type: homeassistant_vehicle
    capabilities:
      soc_pct:
        entity: sensor.car_b_soc
    params:
      capacity_kwh: 40.0
      previous_ids: [ev_b]
""", "") == TEXT
    assert new.index("- id: car_b") > new.index("# refresh_entity")
    assert new.index("- id: car_b") < new.index("- id: meter")
    assert ConfigDocument(new).device_ids() == ["wallbox", "car_a", "car_b", "meter"]


def test_removing_a_device_keeps_the_comments_around_it() -> None:
    doc = ConfigDocument(TEXT)

    new = doc.without_device("wallbox")

    assert ConfigDocument(new).device_ids() == ["car_a", "meter"]
    assert "  # ── Cars ──\n  # the family car\n  - id: car_a\n" in new
    with pytest.raises(ConfigStoreError):
        doc.without_device("nope")


def test_first_device_of_a_file_without_devices() -> None:
    new = ConfigDocument("optimizer:\n  horizon_hours: 24\n").with_device(
        "meter", {"category": "meter", "type": "tasmota", "host": "10.0.0.2"}
    )

    assert new == (
        "optimizer:\n  horizon_hours: 24\n\ndevices:\n"
        "  - id: meter\n    category: meter\n    type: tasmota\n    host: 10.0.0.2\n"
    )


def test_mapping_style_devices_section() -> None:
    text = "devices:\n  meter:\n    category: meter   # root\n    type: tasmota\n    host: a\n"
    doc = ConfigDocument(text)
    entry = doc.entry("meter")
    entry["host"] = "b"

    new = doc.with_device("meter", entry)
    added = ConfigDocument(new).with_device("sub", {"category": "meter", "type": "tasmota"})

    assert new == text.replace("host: a", "host: b")
    assert ConfigDocument(added).entries()["sub"] == {"category": "meter", "type": "tasmota"}


def test_bad_device_id_is_refused() -> None:
    with pytest.raises(ConfigStoreError, match="lower-case"):
        ConfigDocument(TEXT).with_device("My Car", {"category": "vehicle"})


def test_form_round_trip_hides_plain_secrets() -> None:
    specs = {"host": ParamSpec(str), "password": ParamSpec(str, secret=True)}
    entry = {"category": "meter", "type": "tasmota", "host": "a", "password": "plain",
             "params": {"x": 1}, "metered_by": "grid"}

    form = entry_to_form("m", entry, specs)

    assert form["settings"] == {"host": "a", "password": {"!hidden": True}}
    assert form["metered_by"] == "grid" and form["controlled_by"] is None
    assert form_to_entry(form, entry) == entry
    # A secret reference is shown and stored as such.
    form["settings"]["password"] = {"!secret": "meter_pw"}
    assert form_to_entry(form, entry)["password"] == {"!secret": "meter_pw"}
    with pytest.raises(ConfigStoreError, match="no stored value"):
        form_to_entry({**form, "settings": {"password": {"!hidden": True}}}, {})


def test_form_drops_empty_values() -> None:
    entry = form_to_entry({
        "category": "consumer", "type": "generic_consumer", "label": "", "tariff": None,
        "settings": {"a": ""}, "capabilities": {"power_w": {}}, "params": {"x": None},
        "metered_by": "", "learning": True, "ha_discovery": True,
    })

    assert entry == {"category": "consumer", "type": "generic_consumer"}


def test_write_checks_the_revision_and_keeps_backups(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(TEXT)
    store = DeviceConfigStore(path)
    doc = store.read()

    store.write(TEXT + "# v1\n", expected_revision=doc.revision)

    assert path.read_text() == TEXT + "# v1\n"
    with pytest.raises(ConfigConflictError):
        store.write(TEXT + "# v2\n", expected_revision=doc.revision)
    for n in range(KEEP_BACKUPS + 5):
        store.write(TEXT + f"# n{n}\n")
    backups = sorted((tmp_path / "config_backups").glob("config-*.yaml"))
    assert len(backups) == KEEP_BACKUPS
    assert backups[-1].read_text() == TEXT + f"# n{KEEP_BACKUPS + 3}\n"


FLUSH = """\
devices:
- id: car
  category: vehicle
  params:
    capacity_kwh: 40
    charge_curve:
    - soc_pct: 90
      efficiency: 0.9   # full rate
    - soc_pct: 100
      efficiency: 0.55
    previous_ids:
    - ev_car

optimizer:
  horizon_hours: 24   # a day
"""


def test_added_list_items_follow_the_list_style_of_the_file() -> None:
    doc = ConfigDocument(FLUSH)
    entry = doc.entry("car")
    entry["params"]["charge_curve"].append({"soc_pct": 100, "efficiency": 0.5})
    entry["params"]["previous_ids"].append("ev_old")

    new = doc.with_device("car", entry)

    assert _changed_lines(FLUSH, new) == ([], ["      efficiency: 0.5", "    - ev_old"])
    assert new.count("    - soc_pct: 100\n") == 2
    assert ConfigDocument(new).entry("car") == entry


def test_settings_section_is_edited_in_place_added_and_removed() -> None:
    doc = ConfigDocument(FLUSH)

    changed = doc.with_section("optimizer", {"horizon_hours": 36, "step_minutes": 15})
    added = doc.with_section("controller", {"dry_run": True})
    removed = doc.with_section("optimizer", {})

    assert changed == FLUSH.replace("horizon_hours: 24 ", "horizon_hours: 36 ") + "  step_minutes: 15\n"
    assert added == FLUSH + "\ncontroller:\n  dry_run: true\n"
    assert "optimizer" not in removed and removed.startswith("devices:\n- id: car\n")
    assert doc.with_section("optimizer", doc.section("optimizer")) == FLUSH


def test_mapping_collection_entries() -> None:
    text = "tariffs:\n  grid:\n    type: flat_rate   # fixed\n    default: true\n\ndevices: []\n"
    doc = ConfigDocument(text)

    added = doc.with_entry("tariffs", "night", {"type": "flat_rate", "import_price_eur_per_kwh": 0.2})
    only_removed = doc.without_entry("tariffs", "grid")

    assert added == text.replace(
        "    default: true\n",
        "    default: true\n  night:\n    type: flat_rate\n    import_price_eur_per_kwh: 0.2\n",
    )
    assert only_removed == "tariffs: {}\n\ndevices: []\n"
    assert ConfigDocument(added).without_entry("tariffs", "night") == text


def test_secret_references_are_located() -> None:
    doc = ConfigDocument(TEXT + "\ntariffs:\n  spot:\n    type: tibber\n    token: !secret tibber\n")
    assert doc.secret_refs() == {
        "pulse_password": ["devices.meter.password"], "tibber": ["tariffs.spot.token"],
    }


def test_secrets_store_sets_and_deletes_without_touching_the_rest(tmp_path: Path) -> None:
    from nornwatt.config.store import SecretsStore

    path = tmp_path / "secrets.yaml"
    path.write_text("# my secrets\nha_token: abc   # long-lived\nmqtt: 'p w'\n")
    store = SecretsStore(path)

    store.set("tibber", "tok")
    store.set("ha_token", "xyz")
    store.delete("mqtt")

    assert path.read_text() == "# my secrets\nha_token: xyz   # long-lived\ntibber: tok\n"
    assert store.names() == ["ha_token", "tibber"]
    assert path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(ConfigStoreError):
        store.set("bad name", "x")
    with pytest.raises(ConfigStoreError):
        store.set("empty", "")
