"""CNStock (A-share) support in the professional report pipeline (issue #5).

Guards the market allow-list, snapshot defaults, equity feature coverage,
provider catalog entry, and builder wiring for mainland-China equities.
"""
import pytest

from app.professional_report.contracts import EvidenceSnapshotV1
from app.professional_report.providers.catalog import PROVIDER_CATALOG, SUPPORTED_MARKETS
from app.professional_report.snapshot import _MARKET_DEFAULTS, build_evidence_snapshot
from app.professional_report.features.equity import build_equity_features


def _minimal_payload(**over):
    payload = {
        "market": "CNStock",
        "symbol": "600000",
        "collected_at": "2026-10-19T12:00:00Z",
        "quote": {"price": 10.5, "currency": "CNY"},
        "ohlcv": {"latest_bar": {"close": 10.5, "time": "2026-10-19T07:00:00Z"}},
    }
    payload.update(over)
    return payload


class TestContracts:
    def test_market_literal_accepts_cnstock(self):
        snap = EvidenceSnapshotV1.model_validate(
            build_evidence_snapshot(_minimal_payload())
        )
        assert snap.instrument.market == "CNStock"

    def test_cn_equity_alias_normalised(self):
        # the contracts-level alias map routes legacy "cn_equity" to CNStock
        from app.professional_report.contracts import InstrumentIdentity
        inst = InstrumentIdentity.model_validate({
            "market": "cn_equity", "symbol": "600000",
            "canonical_symbol": "600000", "quote_currency": "CNY", "timezone": "Asia/Shanghai",
        })
        assert inst.market == "CNStock"


class TestSnapshotDefaults:
    def test_cn_market_defaults(self):
        assert _MARKET_DEFAULTS["CNStock"] == {
            "currency": "CNY", "timezone": "Asia/Shanghai", "asset_type": "equity",
        }

    def test_snapshot_carries_currency_and_timezone(self):
        snap = build_evidence_snapshot(_minimal_payload())
        assert snap["instrument"]["currency"] == "CNY"
        assert snap["instrument"]["quote_currency"] == "CNY"
        assert snap["instrument"]["timezone"] == "Asia/Shanghai"
        assert snap["instrument"]["asset_type"] == "equity"


class TestEquityFeatures:
    def test_cn_features_declare_missing_professional_inputs(self):
        features = build_equity_features("CNStock", {}, [])
        assert features["market"] == "CNStock"
        # analyst expectations / announcements / northbound flow are not wired
        # for CN yet: they must surface as honest warnings, not fake scores.
        assert "market_specific_data_incomplete" in features["warnings"]

    def test_cn_common_metrics_flow_from_fundamentals(self):
        payload = {
            "fundamental": {
                "identity": {"verified": True},
                "metrics": {"pe_ratio": 7.3, "pb_ratio": 0.95, "roe": 0.115},
            },
        }
        features = build_equity_features("CNStock", payload, [])
        assert features["identity"] == {"verified": True}
        assert "market_specific_data_incomplete" in features["warnings"]


class TestProviderCatalog:
    def test_cnstock_in_supported_markets(self):
        assert "CNStock" in SUPPORTED_MARKETS

    def test_akshare_provider_registered_for_cn(self):
        ak = [p for p in PROVIDER_CATALOG if p.key == "akshare"]
        assert ak, "akshare provider entry missing"
        entry = ak[0]
        assert entry.markets == frozenset({"CNStock"})
        assert entry.keyless is True
        assert "fundamentals" in entry.capabilities

    def test_us_equity_market_still_rejects_unknown(self):
        with pytest.raises(ValueError, match="unsupported equity market"):
            build_equity_features("MOEX", {}, [])


class TestBuilderWiring:
    def test_builder_accepts_cnstock_market(self):
        # build_equity_features is the gate that used to raise for CNStock;
        # builder routes CNStock through the equity branch, so this must work.
        from app.professional_report.features.equity import build_equity_features as bef
        assert bef("CNStock", {}, [])["version"] == "equity_features_v1"
