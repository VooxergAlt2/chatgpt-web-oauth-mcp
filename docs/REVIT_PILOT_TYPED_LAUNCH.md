# Typed QA runner: pre-pilot correction, 3 November 2026

The existing Windows launch policy falsely classified a typed Python launch
as direct Revit launch because its parent path contained `Astra-Revit-Bridge`.
Reproduced with the real Windows QA Python/script paths before the change.

For `windows_App(mode=launch_executable)` the corrected classifier inspects
the basename of **discrete absolute path arguments**. Opaque shell strings,
inline code, quoted strings and arguments containing shell metacharacters
are still inspected in full. Application-name launches retain the previous
policy. The PowerShell launch guard is unchanged.

Actual Revit.exe, case variants, Revit shortcuts, inline Python/PowerShell
launches and explicitly named launch-Revit helpers remain denied. This is
a routing correction, not a replacement for Windows permissions or the
mandatory verified Explorer UIA launch of Revit itself.

Validation:

- Before correction: three new false-positive tests FAIL; seven explicit
  unsafe launch tests PASS.
- After correction: 89 tool-group/workspace tests PASS.
- Full suite: **1765 passed, 2 skipped, 5 warnings**, job
  `job_1793704155236_iFcLC-T9`, exit 0.
- Compilation and `git diff --check`: PASS.

Deployment is a separate gate. This feature must not be called active until
a controlled idle-window cutover reports a new service PID, healthy endpoint
and a live typed-runner smoke test. Do not use an alternate execution channel
to bypass the old service's denial while this patch is merely staged.

Revit acceptance then reuses the existing verified QA process and performs
only the missing actual-mutation rollback test. The Revit test driver uses
workspace-specific request IDs and a run-unique mutation witness, rather than
relying on inner exception strings lost by reflection.
