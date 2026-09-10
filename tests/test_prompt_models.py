"""The local-model manager (spec §3.3): tier by RAM, honest `installed`
against a fake HF cache, and a `download()` that is the ONLY network path —
allow-listed repos, no `*.py`, free-space pre-flight, progress, cancel."""
from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from video_ai_editor.agent.prompt import models
from video_ai_editor.api.jobs import JobCancelled

SEVEN_B = "mlx-community/Qwen2.5-7B-Instruct-4bit"
THREE_B = "mlx-community/Qwen2.5-3B-Instruct-4bit"
GB = models.GB


def make_snapshot(hf_home: Path, model_id: str, *, shards: dict[str, bool], total_size: int | None = 1000,
                  config: bool = True) -> Path:
    """A `models--org--name/snapshots/<rev>/` tree. `shards` maps each file
    the index lists to whether it exists on disk."""
    snap = hf_home / "hub" / f"models--{model_id.replace('/', '--')}" / "snapshots" / "abc123"
    snap.mkdir(parents=True, exist_ok=True)
    if config:
        (snap / "config.json").write_text('{"model_type": "qwen2"}')
    if shards:
        index = {"weight_map": {f"layer{i}": name for i, name in enumerate(shards)}}
        if total_size is not None:
            index["metadata"] = {"total_size": total_size}
        (snap / "model.safetensors.index.json").write_text(json.dumps(index))
        for name, present in shards.items():
            if present:
                (snap / name).write_bytes(b"\0" * 100)
    return snap


def test_allow_patterns_have_no_python():
    assert "*.safetensors" in models.ALLOW_PATTERNS and "*.json" in models.ALLOW_PATTERNS
    assert not any(p.endswith(".py") for p in models.ALLOW_PATTERNS)


def test_tiers_by_ram_and_override(monkeypatch):
    monkeypatch.delenv(models.DEFAULT_MODEL_ENV, raising=False)
    assert models.pick_tier(36 * GB).id == SEVEN_B
    assert models.pick_tier(24 * GB).id == SEVEN_B
    assert models.pick_tier(16 * GB).id == THREE_B
    assert models.pick_tier(8 * GB) is None
    assert models.pick_tier(0) is None
    assert all("0.5B" not in t.id for t in models.MODEL_TIERS)      # degenerate plans — never offered
    custom = models.pick_tier(8 * GB, override="mlx-community/Other-4bit")
    assert custom.id == "mlx-community/Other-4bit" and custom.params == "custom"
    monkeypatch.setenv(models.DEFAULT_MODEL_ENV, THREE_B)
    assert models.pick_tier(36 * GB).id == THREE_B
    assert THREE_B in models.allowed_model_ids() and SEVEN_B in models.allowed_model_ids()


def test_installed_means_config_plus_every_listed_shard(tmp_path):
    make_snapshot(tmp_path, SEVEN_B, shards={"model-00001.safetensors": True, "model-00002.safetensors": False})
    st = models.status(SEVEN_B, hf_home_dir=tmp_path)
    assert not st.installed and st.missing_files == ("model-00002.safetensors",)
    assert st.bytes_on_disk > 0, "bytes on disk is NOT what installed means"
    assert st.expected_bytes == 1000                    # from the index metadata
    assert models.snapshot_dir(SEVEN_B, hf_home_dir=tmp_path) is None
    make_snapshot(tmp_path, SEVEN_B, shards={"model-00001.safetensors": True, "model-00002.safetensors": True})
    st = models.status(SEVEN_B, hf_home_dir=tmp_path)
    assert st.installed and st.missing_files == () and st.snapshot_path.endswith("abc123")
    assert models.snapshot_dir(SEVEN_B, hf_home_dir=tmp_path).is_dir()


def test_single_file_and_missing_config_cases(tmp_path):
    snap = make_snapshot(tmp_path, THREE_B, shards={}, config=True)
    assert not models.status(THREE_B, hf_home_dir=tmp_path).installed          # no model.safetensors yet
    (snap / "model.safetensors").write_bytes(b"\0" * 10)
    assert models.status(THREE_B, hf_home_dir=tmp_path).installed
    assert not models.status(SEVEN_B, hf_home_dir=tmp_path).installed
    assert models.status(SEVEN_B, hf_home_dir=tmp_path).missing_files == ("config.json",)
    assert models.status(SEVEN_B, hf_home_dir=tmp_path).expected_bytes == int(4.3 * GB)


def test_list_models_reports_tier_and_every_known_model(tmp_path, monkeypatch):
    monkeypatch.delenv(models.DEFAULT_MODEL_ENV, raising=False)
    out = models.list_models(ram_bytes=36 * GB, hf_home_dir=tmp_path)
    assert out["tier"] == SEVEN_B and out["ram_gb"] == 36.0
    assert [m["id"] for m in out["models"]] == [SEVEN_B, THREE_B]
    assert all(m["installed"] is False for m in out["models"])
    assert models.list_models(ram_bytes=4 * GB, hf_home_dir=tmp_path)["tier"] is None


def test_download_refuses_unknown_repos_before_any_network(tmp_path, monkeypatch):
    monkeypatch.delenv(models.DEFAULT_MODEL_ENV, raising=False)
    called = []
    with pytest.raises(models.ModelNotAllowed):
        models.download("someone/huge-repo", hf_home_dir=tmp_path, snapshot_download=lambda *a, **k: called.append(1))
    assert not called
    with pytest.raises(models.ModelNotAllowed):
        models.delete("someone/huge-repo", hf_home_dir=tmp_path)


def test_download_passes_allow_patterns_cache_dir_and_progress(tmp_path, monkeypatch):
    monkeypatch.delenv(models.DEFAULT_MODEL_ENV, raising=False)
    seen: dict = {}
    progress: list[float] = []

    def fake_sd(repo_id, **kwargs):
        seen.update(repo_id=repo_id, **kwargs)
        bar = kwargs["tqdm_class"](total=200, disable=True)
        bar.update(50)
        bar.update(150)
        make_snapshot(tmp_path, THREE_B, shards={"model.safetensors": True})
        return str(tmp_path)

    out = models.download(THREE_B, hf_home_dir=tmp_path, snapshot_download=fake_sd, set_progress=progress.append)
    assert seen["repo_id"] == THREE_B
    assert seen["allow_patterns"] == list(models.ALLOW_PATTERNS) and "*.py" not in seen["allow_patterns"]
    assert Path(seen["cache_dir"]) == tmp_path / "hub"
    assert progress[:2] == [0.25, 1.0] and progress[-1] == 1.0
    assert out["status"] == "installed" and out["installed"] is True
    # Second call: already installed, no network.
    again = models.download(THREE_B, hf_home_dir=tmp_path, snapshot_download=lambda *a, **k: pytest.fail("network"))
    assert again["status"] == "already_installed"


def test_download_cancel_and_free_space_preflight(tmp_path, monkeypatch):
    monkeypatch.delenv(models.DEFAULT_MODEL_ENV, raising=False)
    ev = threading.Event()
    ev.set()

    def cancelling_sd(repo_id, **kwargs):
        kwargs["tqdm_class"](total=10, disable=True).update(1)

    with pytest.raises(JobCancelled):
        models.download(THREE_B, hf_home_dir=tmp_path, snapshot_download=cancelling_sd, cancel_event=ev)
    monkeypatch.setattr(models, "_free_bytes", lambda home: int(1.0 * GB))
    with pytest.raises(models.InsufficientDiskSpace):
        models.download(THREE_B, hf_home_dir=tmp_path, snapshot_download=lambda *a, **k: pytest.fail("network"))


def test_download_that_leaves_an_incomplete_snapshot_is_an_error(tmp_path, monkeypatch):
    monkeypatch.delenv(models.DEFAULT_MODEL_ENV, raising=False)

    def partial_sd(repo_id, **kwargs):
        make_snapshot(tmp_path, THREE_B, shards={"a.safetensors": True, "b.safetensors": False})

    with pytest.raises(RuntimeError, match="incomplete"):
        models.download(THREE_B, hf_home_dir=tmp_path, snapshot_download=partial_sd)


def test_delete_removes_only_the_repo_dir(tmp_path, monkeypatch):
    monkeypatch.delenv(models.DEFAULT_MODEL_ENV, raising=False)
    make_snapshot(tmp_path, THREE_B, shards={"model.safetensors": True})
    other = tmp_path / "hub" / "models--Systran--faster-whisper-small"
    other.mkdir(parents=True)
    assert models.delete(THREE_B, hf_home_dir=tmp_path)["status"] == "deleted"
    assert not (tmp_path / "hub" / "models--mlx-community--Qwen2.5-3B-Instruct-4bit").exists()
    assert other.exists()
    assert models.delete(THREE_B, hf_home_dir=tmp_path)["status"] == "not_installed"


_REAL_7B = models._repo_dir(SEVEN_B)


@pytest.mark.skipif(not _REAL_7B.exists(), reason="the 7B snapshot is not in this machine's HF cache")
def test_real_hf_cache_is_found_not_redownloaded(monkeypatch):
    """No fakes: the snapshot already in `~/.cache/huggingface` (4 GB on the
    build Mac) must count as installed, resolve to a DIRECTORY for
    `mlx_lm.load`, and never be offered for download again."""
    monkeypatch.delenv("HF_HOME", raising=False)
    monkeypatch.delenv(models.DEFAULT_MODEL_ENV, raising=False)
    st = models.status(SEVEN_B)
    assert st.installed and st.missing_files == () and st.snapshot_path
    snap = models.snapshot_dir(SEVEN_B)
    assert snap is not None and snap.is_dir() and (snap / "config.json").exists()
    assert st.bytes_on_disk > 3 * GB and st.expected_bytes > 3 * GB
    assert models.download(SEVEN_B, snapshot_download=lambda *a, **k: pytest.fail("network"))["status"] == "already_installed"
    listed = {m["id"]: m for m in models.list_models()["models"]}
    assert listed[SEVEN_B]["installed"] is True and listed[SEVEN_B]["snapshot_path"] == str(snap)


def test_hf_home_honours_env_and_default(monkeypatch, tmp_path):
    monkeypatch.setenv("HF_HOME", str(tmp_path))
    assert models.hub_dir() == tmp_path / "hub"
    monkeypatch.delenv("HF_HOME")
    assert models.hf_home() == Path.home() / ".cache" / "huggingface"
