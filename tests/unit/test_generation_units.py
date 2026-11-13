"""Phase 3 step 3.1: money, calendar, random streams, generation config, ID allocator, number formats."""

from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from scmgen.core.calendar import SimDay, days, fiscal_year, fiscal_year_compact, fiscal_year_label
from scmgen.core.errors import ConfigError
from scmgen.core.money import MoneyError, dec, money, quantity, rate, round_half_up, total, unit_price, whole_rupees
from scmgen.core.rng import RandomStreams, stream_seed
from scmgen.core.settings import DEFAULT_CONFIG_DIR
from scmgen.generation.config import load_generation_config
from scmgen.generation.ids import IdAllocationError, IdAllocator
from scmgen.generation.numbering import DOCUMENT_SERIES, DocumentNumbers, NumberingError
from scmgen.generation.schema_info import PrimaryKey
from scmgen.generation.writer import BatchWriter, WriterError

TODAY = date(2026, 11, 10)


# --------------------------------------------------------------------------- money (D84)


def test_rounding_is_half_up_like_mysql_round():
    assert round_half_up("2.345", 2) == Decimal("2.35")
    assert round_half_up("-2.345", 2) == Decimal("-2.35")        # away from zero, as MySQL ROUND on DECIMAL
    assert round_half_up("2.344999", 2) == Decimal("2.34")
    assert money("10.005") == Decimal("10.01") and str(money(7)) == "7.00"
    assert unit_price("499.99995") == Decimal("500.0000")
    assert quantity("1.0005") == Decimal("1.001")
    assert rate("18") == Decimal("18.0000")
    assert whole_rupees("123.50") == Decimal("124.00") and whole_rupees("123.49") == Decimal("123.00")
    assert total([money("0.10"), money("0.20")]) == Decimal("0.30")


@pytest.mark.parametrize("bad", [0.1, True, float("nan"), "abc", "Infinity", None])
def test_floats_and_non_numbers_are_refused(bad):
    with pytest.raises(MoneyError):
        dec(bad)


def test_values_that_do_not_fit_the_column_are_refused():
    money("9999999999999999.99")                                   # DECIMAL(18,2) maximum
    with pytest.raises(MoneyError):
        money("10000000000000000")
    with pytest.raises(MoneyError):
        rate("1000")


# --------------------------------------------------------------------------- calendar


@pytest.mark.parametrize("day, start_month, year", [
    (date(2026, 4, 1), 4, 2026), (date(2026, 3, 31), 4, 2025), (date(2027, 1, 15), 4, 2026),
    (date(2026, 1, 1), 1, 2026), (date(2026, 6, 30), 7, 2025),
])
def test_fiscal_year(day, start_month, year):
    assert fiscal_year(day, start_month) == year


def test_fiscal_year_labels():
    assert fiscal_year_label(2026) == "2026-27" and fiscal_year_label(2099) == "2099-00"
    assert fiscal_year_label(2026, start_month=1) == "2026"
    assert fiscal_year_compact(2026) == "2627" and fiscal_year_compact(2099) == "9900"


def test_sim_day_window_and_random_times():
    day = SimDay(date(2026, 4, 1))
    assert day.end == datetime(2026, 4, 1, 23, 59, 59, 999999)
    rng = RandomStreams(42).stream(day.day, "dispatch")
    for _ in range(500):
        moment = day.random_time(rng, 10, 19)
        assert day.contains(moment) and 10 <= moment.hour < 19
    assert len(list(days(date(2026, 2, 27), date(2026, 3, 2)))) == 4


# --------------------------------------------------------------------------- random streams (D65)


def test_streams_are_reproducible_and_independent():
    streams = RandomStreams(42)
    first = [streams.stream(date(2026, 4, 1), "orders").random() for _ in range(3)]
    # Drawing from other streams in between changes nothing: each stream depends only on its key.
    streams.stream(date(2026, 4, 1), "payments").random()
    again = [RandomStreams(42).stream(date(2026, 4, 1), "orders").random() for _ in range(3)]
    assert first == again
    seeds = {stream_seed(42, date(2026, 4, 1), "orders"), stream_seed(42, date(2026, 4, 2), "orders"),
             stream_seed(42, date(2026, 4, 1), "orders.b2c"), stream_seed(43, date(2026, 4, 1), "orders")}
    assert len(seeds) == 4


def test_stream_seed_is_a_fixed_function_of_the_key():
    # Pinned value: if this changes, every existing workspace would regenerate different data.
    assert stream_seed(42, date(2026, 4, 1), "orders") == int.from_bytes(
        __import__("hashlib").sha256(b"42|2026-04-01|orders").digest()[:8], "big")


@pytest.mark.parametrize("name", ["Orders", "orders-b2c", "", "1orders", "orders..b2c"])
def test_stream_names_are_checked(name):
    with pytest.raises(ValueError):
        stream_seed(42, date(2026, 4, 1), name)


def test_faker_is_seeded_per_stream():
    streams = RandomStreams(7)
    names = [streams.faker(date(2026, 4, 1), "customers").name() for _ in range(1)]
    other = streams.faker(date(2026, 4, 1), "suppliers").company()
    assert [RandomStreams(7).faker(date(2026, 4, 1), "customers").name()] == names
    assert RandomStreams(7).faker(date(2026, 4, 1), "suppliers").company() == other


# --------------------------------------------------------------------------- generation config


def test_default_config_resolves_medium_two_years_ending_yesterday():
    config = load_generation_config(today=TODAY)
    assert config.preset == "medium" and config.end_date == date(2026, 11, 9)
    assert config.start_date == date(2024, 11, 10) and config.days == 730
    assert config.volumes.orders_per_day == 70 and config.batch_size == 1000
    assert config.gst_rate_revision is True
    assert load_generation_config(today=TODAY, overrides={"gst_rate_revision": False}).gst_rate_revision is False


def test_presets_scale_and_overrides_apply():
    small = load_generation_config(preset="small", today=TODAY)
    large = load_generation_config(preset="large", today=TODAY)
    assert small.volumes.products * 5 == 2000 and large.volumes.products == 10000
    custom = load_generation_config(preset="small", today=TODAY, overrides={
        "volumes.customers_b2b": "500", "history.start_date": "2026-04-01", "batch_size": 250})
    assert custom.volumes.customers_b2b == 500 and custom.start_date == date(2026, 4, 1) and custom.batch_size == 250


def test_hash_is_stable_and_tracks_content():
    a = load_generation_config(preset="small", today=TODAY)
    b = load_generation_config(preset="small", today=TODAY)
    c = load_generation_config(preset="small", today=TODAY, overrides={"volumes.products": 401})
    assert a.config_hash() == b.config_hash() != c.config_hash()
    assert set(a.snapshot()["environment"]) == {"app_version", "python", "faker"}


@pytest.mark.parametrize("overrides, message", [
    ({"volumes.custmers": 5}, "unknown setting"),
    ({"nonsense.x": 1}, "not a settings section"),
    ({"volumes.products": 0}, "between 1"),
    ({"volumes.products": "many"}, "whole number"),
    ({"batch_size": True}, "whole number"),
    ({"history.end_date": "2026-11-10"}, "allow_future"),
    ({"history.start_date": "2026-11-09", "history.end_date": "2026-11-01"}, "after"),
    ({"history.start_date": "2020-01-01"}, "3-year"),
    ({"history.start_date": "01/04/2026"}, "YYYY-MM-DD"),
    ({"history.allow_future": "maybe"}, "true or false"),
    ({"gst_rate_revision": "sometimes"}, "true or false"),
])
def test_invalid_settings_are_refused(overrides, message):
    with pytest.raises(ConfigError, match=message):
        load_generation_config(today=TODAY, overrides=overrides)


def test_sales_settings_load_and_weekday_factors_average_one():
    sales = load_generation_config(preset="small", today=TODAY).sales
    assert sales.cod_share == 0.30 and sales.max_payment_attempts == 3 and sales.b2c_lines[1] == 0.70
    for kind in ("B2B", "B2C"):
        assert abs(sum(sales.weekday_factors[kind]) - 7) < 1e-9                  # rescaled to average 1
        assert len(sales.hours[kind]) == 24
    assert set(sales.b2b_line_value) == {"RETAILER", "WHOLESALER", "CORPORATE", "INSTITUTION", "WORKSHOP"}


@pytest.mark.parametrize("overrides, message", [
    ({"sales.prepaid_methods": {"UPI": 0.5, "CARD": 0.2}}, "add up"),
    ({"sales.prepaid_methods": {"UPI": 0.5, "CHEQUE": 0.5}}, "CHEQUE"),
    ({"sales.hours.B2B": [1, 2, 3]}, "24"),
    ({"sales.weekday_factors.B2C": [0, 0, 0, 0, 0, 0, 0]}, "not all 0"),
    ({"sales.b2c_lines": {0: 1.0}}, "whole numbers"),
    ({"sales.cod_max_value": 500000}, "between"),
    ({"sales.payment_failure_share": 0.9}, "between 0 and 0.5"),
    ({"sales.trend_per_year": 5}, "between -0.5 and 2"),
])
def test_invalid_sales_settings_are_refused(overrides, message):
    with pytest.raises(ConfigError, match=message):
        load_generation_config(today=TODAY, overrides=overrides)


def test_fulfilment_settings_load():
    fulfilment = load_generation_config(preset="small", today=TODAY).fulfilment
    assert fulfilment.dispatch_cutoff_hour == 12 and fulfilment.carrier_pickup_hours == (15, 19)
    assert set(fulfilment.freight_rates) == {"EXPRESS_COURIER", "SURFACE_LTL", "FULL_TRUCKLOAD", "HYPERLOCAL"}
    assert fulfilment.transit_days[0] == (40, 1) and abs(sum(fulfilment.counter_methods.values()) - 1) < 1e-9


@pytest.mark.parametrize("overrides, message", [
    ({"fulfilment.freight_rates.HYPERLOCAL": {"base": 1}}, "base, per_kg and per_kg_100km"),
    ({"fulfilment.transit_days": [[300, 2], [40, 1]]}, "distances must increase"),
    ({"fulfilment.dispatch_cutoff_hour": 18}, "before the carrier pickup"),
    ({"fulfilment.truckload_min_kg": 10}, "above surface_min_kg"),
    ({"fulfilment.counter_methods": {"CASH": 0.5, "CHEQUE": 0.5}}, "CHEQUE"),
    ({"fulfilment.kg_per_package": {"B2B": 25}}, "exactly B2B and B2C"),
])
def test_invalid_fulfilment_settings_are_refused(overrides, message):
    with pytest.raises(ConfigError, match=message):
        load_generation_config(today=TODAY, overrides=overrides)


def test_inventory_settings_load():
    inventory = load_generation_config(preset="small", today=TODAY).inventory
    assert inventory.rebalancing_day == 5 and inventory.transfer_receipt_hours == (7, 8)
    assert inventory.transfer_dispatch_hours == (19, 21) and inventory.min_shelf_days == 30


@pytest.mark.parametrize("overrides, message", [
    ({"inventory.rebalancing_day": 31}, "rebalancing_day"),
    ({"inventory.transit_loss_share": 2}, "transit_loss_share"),
    ({"inventory.transfer_receipt_hours": [9, 7]}, "transfer_receipt_hours"),
    ({"inventory.unknown_knob": 1}, "unknown_knob"),
    ({"inventory.abc_shares": [0.7, 0.5]}, "add up to more than"),
    ({"inventory.repair_cost_share": [0.3, 0.1]}, "min is above max"),
    ({"inventory.handling_damage_per_month": {"DISTRIBUTION_CENTRE": 3}}, "RETAIL_STORE"),
    ({"inventory.disposition_weekday": 7}, "disposition_weekday"),
])
def test_invalid_inventory_settings_are_refused(overrides, message):
    with pytest.raises(ConfigError, match=message):
        load_generation_config(today=TODAY, overrides=overrides)


@pytest.mark.parametrize("overrides, message", [
    ({"expenses.cost_centres": {"RENT": "WAREHOUSE"}}, "no rule for"),
    ({"expenses.sac_codes.RENT": "99A"}, "not a 4/6/8-digit code"),
    ({"expenses.petty_cash_amount": [200, 20000]}, "petty_cash_amount"),
    ({"expenses.electricity_season": [1.0, 1.0]}, "12 monthly factors"),
    ({"expenses.site_costs.RENT": {"WAREHOUSE": [1, 2]}}, "DISTRIBUTION_CENTRE and/or RETAIL_STORE"),
])
def test_invalid_expense_settings_are_refused(overrides, message):
    with pytest.raises(ConfigError, match=message):
        load_generation_config(today=TODAY, overrides=overrides)


def test_future_dates_need_allow_future():
    config = load_generation_config(today=TODAY, overrides={"history.end_date": "2026-12-31",
                                                            "history.allow_future": True})
    assert config.end_date == date(2026, 12, 31)


def test_unknown_preset_and_unknown_yaml_keys(tmp_path: Path):
    with pytest.raises(ConfigError, match="Unknown preset"):
        load_generation_config(preset="huge", today=TODAY)
    text = (DEFAULT_CONFIG_DIR / "generation.yaml").read_text().replace("  batch_size: 1000", "  batchsize: 1000")
    (tmp_path / "generation.yaml").write_text(text)
    with pytest.raises(ConfigError, match="batchsize"):
        load_generation_config(config_dir=tmp_path, today=TODAY)


# --------------------------------------------------------------------------- ID allocator (D20)


def test_ids_continue_after_existing_rows_and_roll_back():
    ids = IdAllocator({"orders": PrimaryKey("orders", "order_id", 2**64 - 1),
                       "states": PrimaryKey("states", "state_id", 65535)}, {"orders": 41})
    assert ids.next("orders") == 42 and ids.next("states") == 1
    saved = ids.checkpoint()
    assert list(ids.block("orders", 3)) == [43, 44, 45]
    ids.restore(saved)                                   # the day was rolled back
    assert ids.next("orders") == 43


def test_id_overflow_and_unknown_tables_are_refused():
    ids = IdAllocator({"states": PrimaryKey("states", "state_id", 65535)}, {"states": 65534})
    ids.next("states")
    with pytest.raises(IdAllocationError, match="maximum"):
        ids.next("states")
    with pytest.raises(IdAllocationError, match="Unknown"):
        ids.next("nope")


# --------------------------------------------------------------------------- writer buffering rules


def test_writer_refuses_inconsistent_rows():
    writer = BatchWriter(["states"])
    writer.insert("states", {"state_id": 1, "state_name": "A"})
    with pytest.raises(WriterError, match="differ"):
        writer.insert("states", {"state_id": 2})
    with pytest.raises(WriterError, match="Unknown table"):
        writer.insert("planets", {"planet_id": 1})
    with pytest.raises(WriterError, match="primary key"):
        writer.update("states", "state_id", 1, {"state_id": 3})
    assert writer.pending_rows == 1
    writer.discard()
    assert writer.pending_rows == 0


class _RecordingConnection:
    """Stands in for a SQLAlchemy connection and records what the writer executes."""

    def __init__(self):
        self.statements: list[tuple[str, int]] = []

    def execute(self, statement, params):
        self.statements.append((str(statement).split(" (")[0], len(params)))

        class _Result:
            rowcount = len(params)
        return _Result()


def test_writer_flushes_parents_before_children_in_batches():
    writer = BatchWriter(["states", "warehouses"], batch_size=2)
    for warehouse_id in (1, 2, 3):                       # children buffered first
        writer.insert("warehouses", {"warehouse_id": warehouse_id, "state_id": 1})
    writer.insert("states", {"state_id": 1})
    writer.update("warehouses", "warehouse_id", 1, {"warehouse_name": "A"})
    writer.update("warehouses", "warehouse_id", 2, {"warehouse_name": "B"})
    writer.update("states", "state_id", 1, {"state_name": "C"})
    conn = _RecordingConnection()
    assert writer.flush(conn) == {"states": 1, "warehouses": 3}
    assert conn.statements == [
        ("INSERT INTO `states`", 1),
        ("INSERT INTO `warehouses`", 2), ("INSERT INTO `warehouses`", 1),        # batch_size 2
        ("UPDATE `warehouses` SET `warehouse_name` = :warehouse_name WHERE `warehouse_id` = :_pk", 2),
        ("UPDATE `states` SET `state_name` = :state_name WHERE `state_id` = :_pk", 1),
    ]



# --------------------------------------------------------------------------- number formats (D38)

# The DDL's CHECK patterns, per document type (see ddl/workspace/*.sql).
DDL_PATTERNS = {
    "TAX_INVOICE": r"^[A-Z]{2}[0-9]{4}-I[0-9]{6}$",
    "CREDIT_NOTE": r"^[A-Z]{2}[0-9]{4}-C[0-9]{6}$",
    "TRANSFER_INVOICE": r"^[A-Z]{2}[0-9]{4}-T[0-9]{6}$",
    "JV": r"^JV/[0-9]{4}-[0-9]{2}/[0-9]{7}$",
}


@pytest.mark.parametrize("document_type", sorted(DOCUMENT_SERIES))
def test_every_series_formats_to_its_ddl_pattern(document_type):
    numbers = DocumentNumbers(fy_start_month=4)
    state = None if document_type == "JV" else "KA"
    value = numbers.format(document_type, 2026, 123, state)
    prefix = DOCUMENT_SERIES[document_type].code
    pattern = DDL_PATTERNS.get(document_type, rf"^{prefix}/[A-Z]{{2}}/[0-9]{{4}}-[0-9]{{2}}/[0-9]{{6}}$")
    assert re.match(pattern, value), value
    if DOCUMENT_SERIES[document_type].style == "tax":
        assert len(value) <= 16                          # GST limit (D38)


def test_examples_from_the_design():
    numbers = DocumentNumbers(fy_start_month=4)
    assert numbers.format("PO", 2026, 123, "KA") == "PO/KA/2026-27/000123"
    assert numbers.format("TAX_INVOICE", 2026, 123, "KA") == "KA2627-I000123"
    assert numbers.format("TRANSFER_INVOICE", 2026, 45, "KA") == "KA2627-T000045"
    assert numbers.format("JV", 2026, 42, None) == "JV/2026-27/0000042"
    with pytest.raises(NumberingError):
        numbers.format("PO", 2026, 1, "ka")
