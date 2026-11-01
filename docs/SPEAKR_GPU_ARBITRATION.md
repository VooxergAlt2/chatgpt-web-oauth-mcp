# Shared GPU admission: Speakr + RAG + local2 reviewer

The incident on 2026-11-01: Qwen3.8-27B's two-slot local reviewer
used ~20,302 MiB on a 22,528 MiB GPU. Speakr requested GPU FP32
alignment/diarization with a 5,000 MiB free-memory gate, receiving
HTTP 503 resource_unavailable on jobs 133 and 134. Once the local
reviewer was stopped, the retry job 135 for recording 54 completed.

No separate GPU manager is introduced. The existing DeM GPU-mode
controller now coordinates Speakr's GPU reservation.

Canonical mutex: the private, same-user advisory lock
~/.local/state/chatgpt-web-oauth-mcp/speakr-gpu-lease.lock.
- Speakr one-shot proxy obtains it before checking GPU memory, keeps it
  through worker cleanup and releases it even after failures/cancellation.
- The GPU-mode controller obtains it before any reviewer-start transition.
  If already held, reviewer mode is rejected with speakr_gpu_lease_busy.
- The controller also reads Speakr's jobs read-only; queued/processing
  jobs reject reviewer mode, checked before, during, and after the transition.
  These checks complement, rather than replace, the advisory lease.
- When Speakr finds insufficient GPU memory, its proxy calls the exact
  controller executable with ensure-asr, which requests canonical rag
  mode and then retries the GPU-memory admission.
- The ensure-asr transition does not interrupt active local2 delegates.
  Existing local2 clients already hold flock on
  /tmp/chatgpt-web-oauth-mcp-local-slots/<sha256-endpoint-prefix>/slot-N.lock.
  The controller acquires ALL matching locks and holds them through
  reviewer shutdown/RAG resume. If not acquired within 240 sec, the
  transition is rejected. It also checks the reviewer /slots endpoint.
- No preemption, model swaps, CPU downgrade or kill/restart of Speakr
  recording workers. Temporary 503 after a bounded refusal is safer than
  corrupting an active request.

The reviewer mode can still be requested manually. The controller rejects
it while Speakr needs GPU. A local model started externally via direct
llama-server not obeying the existing local2 slot lease may lack this
protection; clients must use the canonical runner.

Tests must cover leases held throughout transitions, no slot stealing,
queue races, simulated low-memory recovery, refusal without audio consumption,
and the existing RAG review-mode rollback tests.

The installer is intentionally limited to copying the committed controller
script to ~/.local/libexec/dem-gpu-mode-control; Speakr's own installer
stages only its canonical proxy. Do not modify model, records, templates,
Gemini credentials or existing checkpoint DB.

Rollback: restore the previous controller executable, Speakr proxy package,
user unit and host-asr.env backups, restart only speakr-speaker-proxy when
idle, and leave the canonical GPU mode in rag; source release is independent.
