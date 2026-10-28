"""Out-of-process meshing worker.

Usage: ``python -m opencae.meshing.worker job.json``

Running Gmsh in a separate process keeps the GUI responsive, makes meshing
cancellable (the process can be killed) and isolates the GUI from any crash in
the native mesher. Progress is reported on stdout as ``PROGRESS: <text>``.
"""
from __future__ import annotations

import json
import sys
import time
import traceback
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    if len(argv) != 1:
        print("usage: python -m opencae.meshing.worker job.json", file=sys.stderr)
        return 2
    t0 = time.time()
    try:
        from opencae.meshing.gmsh_mesher import TopologySignatures, generate_mesh
        from opencae.project.io import _from_dict
        from opencae.project.model import MeshSettings

        job = json.loads(Path(argv[0]).read_text())
        settings = _from_dict(MeshSettings, job["settings"])

        def progress(msg: str) -> None:
            print(f"PROGRESS: {msg}", flush=True)

        mesh = generate_mesh(Path(job["brep"]), TopologySignatures.from_dict(job["topology"]), settings,
                             progress, job.get("body_id", ""))
        mesh.save(Path(job["out"]))
        for line in mesh.log[-40:]:
            print(f"LOG: {line}", flush=True)
        print(f"DONE: {mesh.n_nodes} nodes, {mesh.n_elements} elements in {time.time() - t0:.2f} s", flush=True)
        return 0
    except Exception as exc:  # report every failure to the GUI
        print(f"ERROR: {type(exc).__name__}: {exc}", flush=True)
        traceback.print_exc(file=sys.stdout)
        return 1


if __name__ == "__main__":
    sys.exit(main())
