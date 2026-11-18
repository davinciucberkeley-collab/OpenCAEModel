"""Rebuild the bundled K09-L design study (k09l_study.json) with every solver run, so it opens with all
analyses current (FEA, thermal, CFD, assembly, flight), and the design-review website:

    python -m opencae.examples.k09l.build_study [--no-robustness]

Variants: A (as imported), B (revision B, see opencae/engineering/revision_b.py) and C, "Revision C
(recommended)" (revision B plus the changes of opencae/engineering/revision_c.py), all measured against the same
parameters. Also writes the revision-B readiness report, the dashboard report and the revision-B measurement
template (measurements_template_revB.csv: the measurements that close its conditional requirements).
"""
from __future__ import annotations

import argparse
import re
import shutil
import time
from pathlib import Path

from opencae.engineering import manufacturing as MF
from opencae.engineering import report
from opencae.engineering import revision_b as RB
from opencae.engineering import revision_c as RC
from opencae.engineering import study as ST

HERE = Path(__file__).resolve().parent


REV_B_NOTE = "Revision B: outrigger feet, motor sets 6 mm inboard, output limit 0.66, transition procedure, " \
             "shipping estimate"
REV_C_NAME = "Revision C (recommended)"
REV_C_NOTE = ("Revision C (recommended): revision B with the aerodynamic design basis taken from the cruise CFD "
              "(lift slope 2.10/rad, cruise 16.4 m/s) and the transition procedure re-planned for it.")


def build_variants(st: "ST.Study") -> tuple[dict, dict, dict]:
    """Variants A, B and C of the bundled study on ``st`` (no solver runs): returns (A, B, C) revision dicts."""
    ra = st.variants[0]
    ra.update(note="Revision A (as imported from the K09-L Fusion model)", name="Revision A",
              description="The K09-L as imported from the Fusion model (tessellated bodies, BOM, joints).")
    rb = st.add_variant("Revision B", ra["id"], "Revision B: outrigger feet, motor sets 6 mm inboard, motor output "
                        "limit 0.66, transition procedure B, shipping estimate.")
    RB.apply(st.engine)
    st._store(rb["id"])
    rb["note"] = REV_B_NOTE
    rc = st.add_variant(REV_C_NAME, rb["id"], REV_C_NOTE)
    RC.apply(st.engine)
    st._store(rc["id"])
    rc["note"] = REV_C_NAME
    st.baseline = ra["id"]
    return ra, rb, rc


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-robustness", action="store_true")
    ap.add_argument("--workdir", default=None)
    a = ap.parse_args(argv)
    st = ST.Study()
    st.title = "K09-L design study"
    st.path = HERE / "k09l_study.json"  # contour fields go to examples/k09l/fields, next to the study
    wd = Path(a.workdir) if a.workdir else Path.home() / ".opencae" / "k09l_runs"

    def prog(what, i, n):
        print(f"  {what} ({i}/{n})", flush=True)

    t = time.time()
    ra, rb, rc = build_variants(st)
    for v in st.variants:
        print(f"analyses: {v['name']}", flush=True)
        res = st.run_analyses(v["id"], workdir=wd, progress=prog)
        if res["errors"]:
            print("  ERRORS", res["errors"])
        if not a.no_robustness:
            print(f"robustness: {v['name']}", flush=True)
            st.run_robustness(v["id"], progress=prog)
    st.select(rb["id"])
    MF.write_measurement_template(HERE / "measurements_template_revB.csv", st.engine, variant=rb["name"])
    out = st.save(HERE / "k09l_study.json")
    txt = out.read_text(encoding="utf-8")  # bundled study: fields are found by name in examples/k09l/fields
    out.write_text(re.sub(r'"field_file": *"[^"]*"', '"field_file": ""', txt), encoding="utf-8")
    shutil.copyfile(out, RB.STATE_FILE)  # 'Open revision B' in the platform loads this
    report.write_report(st.engine, HERE / "k09l_revB_readiness.html", None, None)
    ST.write_dashboard(st, HERE / "k09l_dashboard.html", renders=True)
    from opencae.web import build as WB
    site = WB.build_site(st, HERE / "website")  # the design-review website (open website/index.html)
    print(f"website: {site}")
    print(f"done in {time.time() - t:.0f} s: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
