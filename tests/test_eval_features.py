"""tools/eval_features.py: the comparison of the feature versions runs on a tiny synthetic configuration, and on a
station database without writing to it."""
import hashlib
import importlib.util
import sys
from pathlib import Path

import numpy as np
import pytest

TOOL = Path(__file__).resolve().parents[1] / "tools" / "eval_features.py"


@pytest.fixture(scope="module")
def tool():
    if not TOOL.is_file():                                      # deployments copy only vinylcheck/ and tests/
        pytest.skip("tools/eval_features.py not present")
    spec = importlib.util.spec_from_file_location("eval_features", TOOL)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["eval_features"] = mod                          # dataclasses look their module up while defining
    spec.loader.exec_module(mod)
    yield mod
    sys.modules.pop("eval_features", None)


def test_synthetic_run_on_a_tiny_configuration(tool):
    lines = []
    res = tool.run_synthetic(recordings=3, seconds=20.0, chains=["budget", "high"], seed=1, floor_seconds=12.0,
                             out=lines.append)
    budget, high = res["profiles"]["budget"], res["profiles"]["high"]
    # the profiles are measured, not copied: the budget chain is noisier and has the higher click floor
    assert budget["chain_db"] > high["chain_db"] + 10 and budget["floor_clicks_per_min"] > high["floor_clicks_per_min"]
    assert budget["ref_dbfs"] == -12.0 and high["ref_dbfs"] == -18.0
    assert set(res["results"]) == {"1", "2"}
    for v in ("1", "2"):
        click = res["results"][v]["click"]
        assert set(click["local"]) == {"budget", "high"} and click["pooled"]["est"] is not None
        assert set(click["transfer"]) == {("budget", "high"), ("high", "budget")}
        assert all(x is not None for x in click["transfer"].values())
        assert click["similarity"][("budget", "high")]["cos"] is not None
    assert res["recommendation"] in ("v1", "v2", "unclear") and res["reasons"]
    assert "Recommendation:" in res["report"] and "transfer budget → high" in res["report"]
    assert len(lines) == 2 and all("click floor" in ln for ln in lines)


def _res(local=None, pooled=None, transfer=None, federated=None):
    """A result of evaluate() for one kind with the given errors (enough for headline and recommend)."""
    return {"local": {"a": {"est": local, "raw": 1.0, "auc": None, "rows": 50}},
            "pooled": None if pooled is None else {"est": pooled}, "transfer": {("a", "b"): transfer},
            "federated": {"b": federated}, "similarity": {}}


def test_recommendation_rules(tool):
    def results(r1, r2):
        empty = _res()
        return {"1": {k: r1 if k == "click" else empty for k in tool.KINDS},
                "2": {k: r2 if k == "click" else empty for k in tool.KINDS}}
    assert tool.recommend(results(_res(0.05, 0.05, 0.30, 0.25), _res(0.05, 0.05, 0.10, 0.08)))[0] == "v2"
    assert tool.recommend(results(_res(0.05, 0.05, 0.10, 0.08), _res(0.09, 0.05, 0.10, 0.08)))[0] == "v1"
    # better across chains but worse locally: not a clear case
    assert tool.recommend(results(_res(0.05, 0.05, 0.30, 0.25), _res(0.09, 0.05, 0.10, 0.08)))[0] == "unclear"
    # a small difference is no difference
    verdict, reasons = tool.recommend(results(_res(0.05, 0.05, 0.10, 0.10), _res(0.05, 0.05, 0.09, 0.095)))
    assert verdict == "unclear" and "no kind is better" in reasons[0] and "click transfer" in reasons[1]
    w = tool.federated_mean([({"intercept": 1.0, "weights": [2.0, 4.0]}, np.array([True, False])),
                             ({"intercept": 3.0, "weights": [4.0, 0.0]}, np.array([True, False]))])
    assert w == {"intercept": 2.0, "weights": [3.0, 0.0]}            # a weight that varied nowhere stays 0
    # several seeds: the means decide, and one seed's lucky difference is not enough
    lucky = tool.heads(results(_res(0.05, 0.05, 0.30, 0.25), _res(0.05, 0.05, 0.10, 0.08)))
    plain = tool.heads(results(_res(0.05, 0.05, 0.10, 0.10), _res(0.05, 0.05, 0.13, 0.13)))
    mean = tool.mean_heads([lucky, plain, plain, plain, plain])
    assert np.isclose(mean["1"]["click"]["transfer"], 0.14) and tool.recommend_heads(mean)[0] == "unclear"
    text = tool.report_repeated([results(_res(0.05, 0.05, 0.30, 0.25), _res(0.05, 0.05, 0.10, 0.08))] * 2, "T")
    assert "v2 − v1 per seed" in text and "Recommendation: Version 2" in text


def _events(rng, n_real, n_music):
    ev = []
    for i in range(n_real):
        ev.append({"t": round(i * 1.7 + rng.uniform(0, 0.2), 4), "kind": "click", "region": "music", "ch": 2,
                   "strength": float(rng.uniform(40, 200)), "amp_dbfs": float(-15 - rng.uniform(20, 35)),
                   "dur_ms": 0.4, "gc": None, "_y": 1})
    for i in range(n_music):
        ev.append({"t": round(0.8 + i * 2.3 + rng.uniform(0, 0.2), 4), "kind": "click", "region": "music", "ch": 3,
                   "strength": float(rng.uniform(30, 120)), "amp_dbfs": float(-15 + rng.uniform(-4, 2)),
                   "dur_ms": 0.9, "gc": None, "_y": 0})
    return ev


def test_real_database_is_only_read(tool, tmp_path):
    from vinylcheck.db import DB
    path = tmp_path / "copy" / "vinyl-check.sqlite"
    db = DB(path)
    rng = np.random.default_rng(5)
    rid = db.add_record("A", "B")
    pids = []
    for i, (chain, ref, floor) in enumerate(((-95.0, -19.0, 20.0), (-78.0, -12.0, 300.0), (None, None, None))):
        pid = db.add_profile(name=f"deck {i}", chain_db=chain)
        if ref is not None:
            db.update_profile(pid, accepted_at=1.0, ref_dbfs=ref, floor_clicks_per_min=floor, floor_ticks_per_min=0.0)
        pids.append(pid)
        for k in range(3):
            rec = db.add_recording(record_id=rid, profile_id=pid, side="A", kind="before", status="analyzed")
            ev = _events(rng, 30 + 5 * k, 20 + 3 * k)
            db.save_analysis(rec, {"metrics": {"music_ref_dbfs": -15.0}, "grading": {"grade": None, "score": None},
                                   "warnings": [], "overview": {},
                                   "events": [{kk: e[kk] for kk in ("t", "dur_ms", "strength", "amp_dbfs", "ch",
                                                                     "kind", "region", "gc")} for e in ev]}, "t", 1.0)
            db.set_master_check(rec, status="done", result={"clicks": {"events": [[e["t"], "v" if e["_y"] else "m"]
                                                                                  for e in ev]}})
    db.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    db.conn.close()
    files = lambda: {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in path.parent.iterdir()}   # noqa: E731
    before = files()
    lines = []
    res = tool.run_real(path, out=lines.append)
    assert files() == before                                    # nothing written, not even a journal
    assert res["profiles"][pids[2]]["missing"] == ["chain_db", "ref_dbfs", "floor_per_min"]
    assert res["profiles"][pids[0]]["missing"] == [] and res["profiles"][pids[0]]["recordings"] == 3
    click = res["results"]["2"]["click"]
    assert set(click["local"]) == {f"profile {pids[0]}", f"profile {pids[1]}"}       # only measured profiles
    assert click["local"][f"profile {pids[0]}"]["rows"] == res["results"]["1"]["click"]["local"][
        f"profile {pids[0]}"]["rows"]                                                 # both versions, same examples
    assert len(click["transfer"]) == 2 and "Recommendation:" in res["report"]
    assert any("without a complete zero point" in ln for ln in lines)
