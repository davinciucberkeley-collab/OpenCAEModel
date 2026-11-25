Update the implementation requirements for my OpenCAE Studio command interface.

Add two dockable side panels to the existing application.

- Left panel — Progress Monitor.
- Center — Existing OpenCAE workspace.
- Right panel — Command Console.

Preserve the current workspace, menus, toolbars, execution controls, model tree, viewport, analysis setup, and results workflow. Integrate the two new panels alongside them. If existing docks occupy these locations, arrange the new docks without hiding or replacing existing tools.

Both panels must be resizable, collapsible, and restorable through the View menu. Save their layout between sessions.

LEFT PANEL — PROGRESS MONITOR

Give me one place to monitor the entire active workflow, including operations started from the command console and the existing GUI.

Show the active project, current operation, queued work, completed steps, failed steps, and overall workflow status.

For an analysis, display relevant stages such as:

- Input validation.
- Geometry preparation.
- Meshing.
- Solver execution.
- Postprocessing.
- Results ready.

Show the active component or assembly, analysis type, elapsed time, and available progress information.

Use actual backend progress when available. For stages without measurable progress, show an activity indicator and elapsed time. Calculate overall percentages only when there is a defensible basis.

For parameter studies, show completed cases out of the total, the current case, and failed cases. Allow individual cases to be expanded.

Keep the default view concise. Let me expand an operation to inspect its logs, warnings, inputs, and outputs.

Provide applicable controls to cancel, retry a failed operation, and open completed results. Enable pause or resume only when the backend supports it.

Switching between projects should preserve each project's job history and make any background activity visible.

RIGHT PANEL — COMMAND CONSOLE

Provide the friendly command interface described earlier.

Support project opening, selection, analysis execution, component connections, motion commands, parameter studies, and dashboards through the existing application services.

Examples:

open gripper
select left finger
run structural on left finger
run thermal on motor housing
run fluid around assembly
run motion on gripper
iterate finger thickness from 2 mm to 5 mm in 10 steps
show displacement
open dashboard

Include autocomplete, command history, concise responses, and clickable suggestions.

After accepting a command, send its live execution status to the left Progress Monitor. Keep the command response brief, with a link to the corresponding operation.

If a command needs more information, ask the clarification in the right panel. Once the user completes the setup, start execution and show progress on the left.

SHARED APPLICATION STATE

The panels must connect to the same project state, selections, solver jobs, and results as the existing GUI.

For example:

1. I select a component in the existing viewport.
2. I type “run structural” in the right console.
3. The existing analysis service executes the study.
4. The left monitor shows its progress.
5. Results open in the existing results viewer.

The same progress must appear if I start the analysis with the existing Run button.

Create one shared job registry and event flow, reusing existing infrastructure where available. Avoid duplicate solver launches, inconsistent selections, or separate project copies.

Keep long operations off the GUI thread. Preserve readable job status after failures and cancellation.

IMPLEMENTATION AND VERIFICATION

Inspect the existing dock layout and execution pipeline first, then implement the additions.

Verify that:

- Both panels appear alongside the existing workspace.
- Existing controls remain accessible and functional.
- Commands and GUI controls execute through the same services.
- Both execution paths update the left monitor.
- Results open through the existing viewer.
- Collapsing either panel restores usable workspace space.
- Layout and command history survive reopening.
- Cancellation, failures, and multiple jobs display correctly.

Deliver the implemented changes, launch instructions, and a short demonstration of one analysis started from the command console and one started from the existing GUI.