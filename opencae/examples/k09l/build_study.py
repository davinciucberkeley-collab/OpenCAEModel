"""Rebuild the bundled K09-L design study (k09l_study.json) with every solver run, so it opens with all
analyses current:

    python -m opencae.examples.k09l.build_study [--no-robustness]

Variants: A (as imported), B (revision B, see opencae/engineering/revision_b.py) and an example variant C
(B carrying a 0.3 kg payload) to show how a new variant is measured against the same parameters.
Also writes the revision-B readiness report and an example dashboard report.
"""
from __future__ import annotations

import argparse
import shutil
import time
from pathlib import Path

from opencae.engineering import report
from opencae.engineering import revision_b as RB
from opencae.engineering import study as ST

HERE = Path(__file__).resolve().parent


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-robustness", action="store_true")
    ap.add_argument("--workdir", default=None)
    a = ap.parse_args(argv)
    st = ST.Study()
    st.title = "K09-L design study"
    ra = st.variants[0]
    ra.update(note="Revision A (as imported from the K09-L Fusion model)", name="Revision A",
              description="The K09-L as imported from the Fusion model (tessellated bodies, BOM, joints).")
    wd = Path(a.workdir) if a.workdir else Path.home() / ".opencae" / "k09l_runs"

    def prog(what, i, n):
        print(f"  {what} ({i}/{n})", flush=True)

    t = time.time()
    rb = st.add_variant("Revision B", ra["id"], "Revision B: outrigger feet, motor sets 6 mm inboard, motor output "
                        "limit 0.66, transition procedure B, shipping estimate.")
    RB.apply(st.engine)
    st._store(rb["id"])
    rb["note"] = "Revision B: outrigger feet, motor sets 6 mm inboard, output limit 0.66, transition procedure, " \
                 "shipping estimate"
    rc = st.add_variant("C: B + 0.3 kg payload", rb["id"], "Example variant: revision B carrying a 0.3 kg payload "
                        "at the battery (created in the Design Monitor with New variant + Edit).")
    st.edit(rc["id"], params={"payload": 0.3})
    st.baseline = ra["id"]
    for v in st.variants:
        print(f"analyses: {v['name']}", flush=True)
        res = st.run_analyses(v["id"], workdir=wd, progress=prog)
        if res["errors"]:
            print("  ERRORS", res["errors"])
        if not a.no_robustness:
            print(f"robustness: {v['name']}", flush=True)
            st.run_robustness(v["id"], progress=prog)
    st.select(rb["id"])
    out = st.save(HERE / "k09l_study.json")
    shutil.copyfile(out, RB.STATE_FILE)  # 'Open revision B' in the platform loads this
    report.write_report(st.engine, HERE / "k09l_revB_readiness.html", None, None)
    ST.write_dashboard(st, HERE / "k09l_dashboard.html", renders=True)
    print(f"done in {time.time() - t:.0f} s: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
