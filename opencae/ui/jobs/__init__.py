"""Progress Monitor: one shared job registry and the left-hand dock that shows it.

``registry`` is Qt-free. ``install(main_window)`` (called by ``MainWindow._install_extensions``) adds the dock, the
View menu entry and the status-bar counter. ``track_callable`` runs a function in a background thread as a job.
"""
from __future__ import annotations

from opencae.ui.jobs.registry import (ACTIVE, ANALYSIS_STAGES, FINAL, Case, Event, Job, JobRegistry, Stage,  # noqa: F401
                                      S_GEOMETRY, S_MESH, S_POST, S_READY, S_SOLVER, S_VALIDATE, default,
                                      set_default, standard_stages)


def install(main_window):
    from opencae.ui.jobs.monitor import JobsExtension
    return JobsExtension(main_window)


def track_callable(main_window, kind, title, target, fn, stages=None, source="gui", *, analysis_type="",
                   inputs=None, project_path=None, retry=True, allow_duplicate=False, on_done=None,
                   on_failed=None, opener=None, outputs_from=None):
    """Run ``fn(progress, cancelled)`` in a background thread as a monitored job; returns the ``Job``.

    ``progress(fraction_or_None, message="")`` reports real progress (never guess); ``cancelled()`` is polled by
    the work. ``fn`` may return a dict, which becomes the job outputs. If the same kind and target already runs
    in the project, nothing is started: the existing job is focused in the monitor and returned.
    """
    from opencae.ui.jobs.monitor import track_callable as _impl
    return _impl(main_window, kind, title, target, fn, stages=stages, source=source, analysis_type=analysis_type,
                 inputs=inputs, project_path=project_path, retry=retry, allow_duplicate=allow_duplicate,
                 on_done=on_done, on_failed=on_failed, opener=opener, outputs_from=outputs_from)


def track_mbd_run(main_window, project, study, *, data_dir=None, project_path=None, source="gui",
                  on_done=None, on_failed=None):
    """Adapter for multibody runs: runs ``opencae.mbd.run.run_study(project, study, progress, cancelled)`` as a job.

    Progress is the solver's own ``step / n_steps``. The result opens with the opener registered under
    ``"mbd.playback"`` (``registry.register_opener``)."""
    def work(progress, cancelled):
        from opencae.mbd.run import run_study
        kw = {"data_dir": data_dir} if data_dir is not None else {}
        run_study(project, study, progress=lambda f, msg="": progress(f, msg), cancelled=cancelled, **kw)
        return {"study": getattr(study, "name", ""), "opener": "mbd.playback", "study_id": getattr(study, "id", "")}

    name = getattr(study, "name", "") or "motion study"
    return track_callable(main_window, "motion", f"Motion run: {name}", name, work,
                          stages=("Input validation", "Solver", "Postprocessing", "Results ready"), source=source,
                          analysis_type="mbd", project_path=project_path, on_done=on_done, on_failed=on_failed,
                          inputs={"study": name})
