"""Sweep-account cross-proof: verify, catch missing companions, surface
orphans and foreign activity."""

import io

from finance_helper.recon import bank, sweep

BANK = "samples/bank_sample.csv"
SWEEP = "samples/sweep_sample.csv"


def test_cross_proof_verifies_matched_transfer_and_surfaces_rest():
    main = bank.load_bank_csv(BANK)
    side = bank.load_bank_csv(SWEEP)
    res = sweep.cross_proof(main, side)
    assert res["checked"] == 1 and res["verified"] == 1 and res["unverified"] == 0
    # The -2000 sweep-side transfer has no checking counterpart.
    assert len(res["orphans"]) == 1 and res["orphans"][0]["amount"] == "-2000"
    # Interest + the wire out are not transfers with checking.
    descs = " ".join(r["desc"] for r in res["foreign"])
    assert "SUNSET HOLDINGS" in descs and "INTEREST" in descs
    # Verified sweep stays whatever status the engine gave it (not exception).
    sw = next(t for t in main if t.kind == "sweep")
    assert sw.status != "exception"


def test_cross_proof_flags_sweep_that_never_arrived(tmp_path):
    main = bank.load_bank_csv(BANK)
    # Sweep statement missing the +5000 companion row.
    lines = [l for l in open(SWEEP).read().splitlines() if "9613" not in l or "-2000" in l]
    p = tmp_path / "sweep.csv"
    p.write_text("\n".join(lines) + "\n")
    side = bank.load_bank_csv(str(p))
    res = sweep.cross_proof(main, side)
    assert res["unverified"] == 1
    sw = next(t for t in main if t.kind == "sweep")
    assert sw.status == "exception"
    assert "NO companion movement" in sw.reason


def test_sweep_sample_integrity_is_consistent():
    chk = bank.integrity_check(SWEEP)
    assert chk["ok"] and not chk["breaks"]


def test_web_run_with_sweep_file(tmp_path):
    import pytest
    from finance_helper.web.app import RUNS, create_app

    RUNS.clear()
    app = create_app()
    app.config.update(TESTING=True)
    with app.test_client() as client:
        data = {
            "bank_file": (io.BytesIO(open(BANK, "rb").read()), "bank.csv"),
            "sweep_file": (io.BytesIO(open(SWEEP, "rb").read()), "sweep.csv"),
        }
        resp = client.post("/cashproof/run", data=data,
                           content_type="multipart/form-data")
        assert resp.status_code == 302
        run_id = resp.headers["Location"].rstrip("/").split("/")[-1]
        body = client.get(f"/cashproof/{run_id}").data
        assert b"Sweep account cross-proof" in body
        assert b"1/1" in body
        assert b"SUNSET HOLDINGS" in body
    RUNS.clear()
