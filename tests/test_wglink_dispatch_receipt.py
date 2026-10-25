"""False-return delivery, real delivery loss, cancellation and startup bounds."""
import threading
import time

import pytest

from test_wglink_launcher import _checked_fixture, _advertise_launcher, _installed_entry, _run, _inbox
import wglink_launcher


def false_return(fixture, monkeypatch):
    def fire(self, event_id, _payload=""):
        object.__getattribute__(self, "fired").append(event_id)
        return False
    monkeypatch.setattr(type(fixture.app), "fireCustomEvent", fire)


@pytest.mark.parametrize("operation", ["send", "solve"])
def test_false_return_delivered_event_resumes_once_on_main(monkeypatch, tmp_path, operation):
    fixture = _checked_fixture(monkeypatch, tmp_path, "WGLink_false_delivered")
    _advertise_launcher(fixture, [str(_installed_entry(tmp_path))])
    false_return(fixture, monkeypatch)
    launcher = fixture.module.wglink_launcher
    monkeypatch.setattr(launcher, "collecting", lambda *args: False)
    starts = []
    receipts = []

    def ensure(*args, **kwargs):
        assert receipts and receipts[0].received.is_set(), "launcher ran before actual main-thread receipt"
        assert 0 < kwargs["timeout"] <= launcher.ENSURE_TIMEOUT
        starts.append(args)

    monkeypatch.setattr(launcher, "ensure_backend", ensure)
    _run(fixture.module, operation)
    worker = fixture.module._backend_admission.thread
    deadline = time.monotonic() + 2
    while worker.is_alive() and time.monotonic() < deadline:
        if not receipts:
            with fixture.module._followups_lock:
                if fixture.module._followups:
                    receipts.append(fixture.module._followups[0])
            if not receipts:
                time.sleep(0.002)
                continue
        fixture.module.FollowupEventHandler().notify(None)
        time.sleep(0.002)
    worker.join(timeout=0.1)
    assert not worker.is_alive()
    fixture.module.FollowupEventHandler().notify(None)
    fixture.module.FollowupEventHandler().notify(None)
    assert len(starts) == 1 and len(fixture.exports) == 1
    assert len(_inbox(fixture.ipc)) == 1, fixture.ui.messages
    assert _inbox(fixture.ipc)[0]["kind"] == ("prepare_and_solve" if operation == "solve" else "receive_snapshot")


def test_false_return_without_delivery_cancels_late_job_and_reports_loss(monkeypatch, tmp_path):
    fixture = _checked_fixture(monkeypatch, tmp_path, "WGLink_false_undelivered")
    _advertise_launcher(fixture, [str(_installed_entry(tmp_path))])
    false_return(fixture, monkeypatch)
    launcher = fixture.module.wglink_launcher
    monkeypatch.setattr(launcher, "collecting", lambda *args: False)
    monkeypatch.setattr(launcher, "DISPATCH_TIMEOUT", 0.03)
    monkeypatch.setattr(launcher, "ensure_backend", lambda *a, **k: pytest.fail("undelivered startup launched WG"))
    _run(fixture.module, "send")
    deadline = time.monotonic() + 1
    while not fixture.module._followups and time.monotonic() < deadline:
        time.sleep(0.001)
    late = fixture.module._followups[0]
    fixture.module._backend_admission.thread.join(timeout=1)
    assert not fixture.module._backend_admission.thread.is_alive()
    assert fixture.module._followups == []
    late()
    assert fixture.exports == [] and fixture.ui.progress_dialogs == [] and _inbox(fixture.ipc) == []
    first_worker = fixture.module._backend_admission.thread
    _run(fixture.module, "send")
    assert fixture.module._backend_admission.thread is first_worker
    assert "Start Waveguide Generator manually" in fixture.ui.messages[-1][1]
    assert "not delivered before its deadline" in "\n".join(fixture.ui.text_palette.written)


def test_receipt_is_exactly_once_and_acknowledges_before_job():
    ran = []
    receipt = wglink_launcher.DispatchReceipt(lambda: ran.append(receipt.received.is_set()))
    receipt()
    receipt()
    assert ran == [True]
    receipt.wait(lambda: True, time.monotonic() - 1)
    assert receipt.cancel() is False


@pytest.mark.parametrize("hold_worker_exit", [False, True])
def test_lost_completion_is_reported_and_hides_start_status_on_main(monkeypatch, tmp_path, hold_worker_exit):
    fixture = _checked_fixture(monkeypatch, tmp_path, "WGLink_lost_completion")
    _advertise_launcher(fixture, [str(_installed_entry(tmp_path))])
    false_return(fixture, monkeypatch)
    launcher = fixture.module.wglink_launcher
    monkeypatch.setattr(launcher, "collecting", lambda *args: False)
    monkeypatch.setattr(launcher, "DISPATCH_TIMEOUT", 0.1)
    monkeypatch.setattr(launcher, "ensure_backend", lambda *a, **k: None)
    finished, release = threading.Event(), threading.Event()
    if hold_worker_exit:
        class DelayedExit(threading.Thread):
            def run(self):
                super().run()
                finished.set()
                release.wait(2)
        monkeypatch.setattr(launcher.threading, "Thread", DelayedExit)
    _run(fixture.module, "send")
    deadline = time.monotonic() + 1
    while not fixture.module._followups and time.monotonic() < deadline:
        time.sleep(0.001)
    # Pump exactly the starting event, then lose the completion event.
    fixture.module.FollowupEventHandler().notify(None)
    worker = fixture.module._backend_admission.thread
    try:
        if hold_worker_exit:
            assert finished.wait(1)
            assert worker.is_alive()
        else:
            worker.join(timeout=1)
            assert not worker.is_alive()
        assert fixture.ui.progress_dialogs and fixture.module._backend_progress is not None
        assert fixture.exports == [] and _inbox(fixture.ipc) == []
        _run(fixture.module, "send")
        assert "Start Waveguide Generator manually" in fixture.ui.messages[-1][1]
        assert fixture.module._backend_progress is None
        assert fixture.ui.progress_dialogs[0].events[-1][0] == "hide"
    finally:
        release.set()
        worker.join(timeout=1)


@pytest.mark.parametrize("cancelled", [False, True])
def test_undelivered_receipt_cannot_run_after_timeout_or_cancel(cancelled):
    ran = []
    receipt = wglink_launcher.DispatchReceipt(lambda: ran.append(True))
    with pytest.raises(wglink_launcher.LauncherError, match="cancelled" if cancelled else "deadline"):
        receipt.wait(lambda: cancelled, time.monotonic() - 1)
    receipt()
    assert ran == []


def test_expired_admission_cannot_spawn_and_retains_original_bound(monkeypatch, tmp_path):
    now = [0.0]
    monkeypatch.setattr(wglink_launcher.time, "monotonic", lambda: now[0])
    def collecting(*args):
        now[0] = wglink_launcher.DISCOVERY_TIMEOUT + wglink_launcher.ENSURE_TIMEOUT + 1
        return False
    monkeypatch.setattr(wglink_launcher, "collecting", collecting)
    monkeypatch.setattr(wglink_launcher, "ensure_backend", lambda *a, **k: pytest.fail("expired admission spawned"))
    admission = wglink_launcher.BackendAdmission()
    outcomes = []
    admission.request(tmp_path, tmp_path, [], None, outcomes.append, lambda job: job())
    admission.thread.join(timeout=1)
    assert len(outcomes) == 1 and "admission reached its deadline" in outcomes[0]
    assert admission.dispatch_deadline == 76
