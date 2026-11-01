"""Cross-service GPU admission contracts. No real GPU, jobs or systemd calls."""

from __future__ import annotations

import importlib.util
from contextlib import contextmanager
import fcntl
import hashlib
import os
import io
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
CONTROLLER = ROOT / "scripts/dem_gpu_mode_control.py"


def controller(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("DEM_GPU_SPEAKR_JOB_DB", str(tmp_path / "missing.db"))
    monkeypatch.setenv("DEM_GPU_SPEAKR_LEASE_PATH", str(tmp_path / "gpu-lease.lock"))
    spec = importlib.util.spec_from_file_location("dem_gpu_speakr_fixture", CONTROLLER)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_only_live_speakr_jobs_block_reviewer(monkeypatch, tmp_path: Path):
    m = controller(monkeypatch, tmp_path)
    path = tmp_path / "queue.db"
    with sqlite3.connect(path) as db:
        db.execute("create table processing_job (id integer,recording_id integer,status text)")
        db.executemany("insert into processing_job values (?,?,?)", [
            (1, 9, "completed"), (2, 10, "failed"),
            (3, 11, "queued"), (4, 12, "processing"),
        ])
    monkeypatch.setattr(m, "SPEAKR_JOB_DB", path)
    assert m._inflight_speakr_jobs() == [
        {"job_id": 3, "recording_id": 11, "status": "queued"},
        {"job_id": 4, "recording_id": 12, "status": "processing"},
    ]
    path.write_text("not a database")
    with pytest.raises(RuntimeError, match="speakr_job_ledger_unavailable"):
        m._inflight_speakr_jobs()


def test_reviewer_rejects_live_speakr_without_gpu_changes(monkeypatch, tmp_path: Path):
    m=controller(monkeypatch,tmp_path); actions=[]
    monkeypatch.setattr(m,"_model_ready",lambda:True)
    monkeypatch.setattr(m,"_mode",lambda:"rag")
    monkeypatch.setattr(m,"_maintenance_available",lambda:True)
    monkeypatch.setattr(m,"_inflight_speakr_jobs",lambda:[{"job_id":135,"recording_id":54,"status":"processing"}])
    monkeypatch.setattr(m,"_post_json",lambda *a,**k:actions.append("post"))
    monkeypatch.setattr(m,"_run_user_systemctl",lambda *a,**k:actions.append("systemctl"))
    monkeypatch.setattr(m,"_write_state",lambda **kw:actions.append(kw))
    assert m._switch_to_reviewer()==2
    assert actions == [{"target":"reviewer","status":"rejected","error":"speakr_job_inflight",
                        "details":{"speakr_jobs":[{"job_id":135,"recording_id":54,"status":"processing"}]}}]


def test_reviewer_rolls_back_when_speakr_claims_during_suspend(monkeypatch,tmp_path:Path):
    m=controller(monkeypatch,tmp_path);actions=[]
    jobs=iter([[],[{"job_id":135,"recording_id":54,"status":"processing"}]])
    monkeypatch.setattr(m,"_model_ready",lambda:True)
    monkeypatch.setattr(m,"_mode",lambda:"rag")
    monkeypatch.setattr(m,"_maintenance_available",lambda:True)
    monkeypatch.setattr(m,"_inflight_speakr_jobs",lambda:next(jobs))
    monkeypatch.setattr(m,"_inflight_source_runs",lambda:[])
    monkeypatch.setattr(m,"_active_rag_background_services",lambda:[])
    monkeypatch.setattr(m,"_wait_rag_state",lambda *a:True)
    monkeypatch.setattr(m,"_wait_http",lambda *a:True)
    monkeypatch.setattr(m,"_run_user_systemctl",
                        lambda *a,**k:actions.append(("unit",a)) or subprocess.CompletedProcess(a,0,"",""))
    monkeypatch.setattr(m,"_post_json",lambda url,**k:actions.append(("post",url)) or {"ok":True})
    monkeypatch.setattr(m,"_write_state",lambda **kw:actions.append(("state",kw)))
    assert m._switch_to_reviewer()==1
    assert [value for kind,value in actions if kind=="post"]==[m.RAG_SUSPEND,m.RAG_RESUME]
    assert not any(kind=="unit" and value==("start",m.REVIEWER_UNIT) for kind,value in actions)
    assert any(kind=="state" and "speakr_job_raced_suspend" in value.get("error","") for kind,value in actions)


def test_reviewer_rolls_back_when_speakr_claims_during_load(monkeypatch,tmp_path:Path):
    m=controller(monkeypatch,tmp_path);actions=[]
    jobs=iter([[],[],[{"job_id":135,"recording_id":54,"status":"processing"}]])
    monkeypatch.setattr(m,"_model_ready",lambda:True)
    monkeypatch.setattr(m,"_mode",lambda:"rag")
    monkeypatch.setattr(m,"_maintenance_available",lambda:True)
    monkeypatch.setattr(m,"_inflight_speakr_jobs",lambda:next(jobs))
    monkeypatch.setattr(m,"_inflight_source_runs",lambda:[])
    monkeypatch.setattr(m,"_active_rag_background_services",lambda:[])
    monkeypatch.setattr(m,"_wait_rag_state",lambda *a:True)
    monkeypatch.setattr(m,"_wait_user_unit",lambda *a:True)
    monkeypatch.setattr(m,"_wait_http",lambda *a:True)
    monkeypatch.setattr(m,"_run_user_systemctl",
                        lambda *a,**k:actions.append(("unit",a)) or subprocess.CompletedProcess(a,0,"",""))
    monkeypatch.setattr(m,"_post_json",lambda url,**k:actions.append(("post",url)) or {"ok":True})
    monkeypatch.setattr(m,"_write_state",lambda **kw:actions.append(("state",kw)))
    assert m._switch_to_reviewer()==1
    assert ("unit",("start",m.REVIEWER_UNIT)) in actions
    assert ("unit",("stop",m.REVIEWER_UNIT)) in actions
    assert any(kind=="state" and "speakr_job_raced_reviewer_start" in value.get("error","") for kind,value in actions)


def test_ensure_asr_switches_only_after_idle(monkeypatch,tmp_path:Path):
    m=controller(monkeypatch,tmp_path);actions=[]
    path=tmp_path/"gpu-mode.request";path.write_text("reviewer\n")
    monkeypatch.setattr(m,"REQUEST_PATH",path)
    monkeypatch.setattr(m,"_mode",lambda:"reviewer")
    monkeypatch.setattr(m,"_user_unit_active",lambda unit:True)
    @contextmanager
    def admitted(seconds):
        actions.append("idle")
        yield True
    monkeypatch.setattr(m,"_hold_reviewer_admission",admitted)
    monkeypatch.setattr(m,"_switch_to_rag",lambda:actions.append("rag") or 0)
    assert m._ensure_asr_mode()==0
    assert actions==["idle","rag"]
    assert path.read_text()=="rag\n"
    assert path.stat().st_mode & 0o777 == 0o600


def test_ensure_asr_never_interrupts_busy_reviewer(monkeypatch,tmp_path:Path):
    m=controller(monkeypatch,tmp_path);states=[]
    path=tmp_path/"gpu-mode.request";path.write_text("reviewer\n")
    monkeypatch.setattr(m,"REQUEST_PATH",path)
    monkeypatch.setattr(m,"_mode",lambda:"reviewer")
    monkeypatch.setattr(m,"_user_unit_active",lambda unit:True)
    @contextmanager
    def rejected(seconds):
        yield False
    monkeypatch.setattr(m,"_hold_reviewer_admission",rejected)
    monkeypatch.setattr(m,"_switch_to_rag",lambda:pytest.fail("must not interrupt busy reviewer"))
    monkeypatch.setattr(m,"_write_state",lambda **kw:states.append(kw))
    assert m._ensure_asr_mode()==2
    assert path.read_text()=="reviewer\n"
    assert states[-1]["error"]=="reviewer_busy_for_speakr"


def test_slot_idle_requires_both_slots_idle(monkeypatch,tmp_path:Path):
    m=controller(monkeypatch,tmp_path)
    class Response:
        status=200
        def __init__(self,payload):self.payload=payload
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def read(self,_):return json.dumps(self.payload).encode()
    for payload, expected in [
        ([{"is_processing":False,"state":0},{"is_processing":False,"state":0}],True),
        ([{"is_processing":True,"state":1},{"is_processing":False,"state":0}],False),
        ([{"state":1}],False), ([],False),
        ([{"n_ctx":1024}],False),
    ]:
        with patch.object(m.urlrequest,"urlopen",return_value=Response(payload)):
            assert m._reviewer_idle() is expected


def test_exclusive_speech_lease_prevents_reviewer_start(monkeypatch,tmp_path:Path):
    m=controller(monkeypatch,tmp_path);actions=[]
    path=m.SPEAKR_GPU_LEASE_PATH
    fd=os.open(path,os.O_CREAT|os.O_RDWR,0o600)
    try:
        fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        monkeypatch.setattr(m,"_write_state",lambda **kw:actions.append(kw))
        monkeypatch.setattr(m,"_switch_to_reviewer_locked",
                            lambda:pytest.fail("never suspend RAG with active ASR lease"))
        assert m._switch_to_reviewer()==2
        assert actions[-1]["error"]=="speakr_gpu_lease_busy"
    finally:
        fcntl.flock(fd,fcntl.LOCK_UN);os.close(fd)


def test_reviewer_slot_flocks_are_held_through_mode_switch(monkeypatch,tmp_path:Path):
    m=controller(monkeypatch,tmp_path)
    monkeypatch.setattr(m.tempfile,"gettempdir",lambda:str(tmp_path))
    monkeypatch.setattr(m,"_user_unit_active",lambda unit:True)
    monkeypatch.setattr(m,"_read_json",lambda url:{"total_slots":2})
    monkeypatch.setattr(m,"_reviewer_idle",lambda:True)
    key=hashlib.sha256(b"http://127.0.0.1:8083").hexdigest()[:16]
    folder=tmp_path/"chatgpt-web-oauth-mcp-local-slots"/key
    with m._hold_reviewer_admission(2) as admitted:
        assert admitted
        for slot_id in range(2):
            path=folder/f"slot-{slot_id}.lock"
            fd=os.open(path,os.O_RDWR)
            try:
                with pytest.raises(BlockingIOError):
                    fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            finally:os.close(fd)
    for slot_id in range(2):
        fd=os.open(folder/f"slot-{slot_id}.lock",os.O_RDWR)
        try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        finally:os.close(fd)


def test_ensure_asr_rag_ready_is_noop(monkeypatch,tmp_path:Path):
    m=controller(monkeypatch,tmp_path)
    monkeypatch.setattr(m,"_mode",lambda:"rag")
    monkeypatch.setattr(m,"_wait_http",lambda url,timeout:True)
    monkeypatch.setattr(m,"_switch_to_rag",lambda:pytest.fail("should not switch"))
    assert m._ensure_asr_mode()==0
