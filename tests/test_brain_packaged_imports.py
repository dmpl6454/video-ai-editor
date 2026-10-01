"""EB1-D INBUILT: the whole graph builds with librosa, torch and mlx blocked in
`sys.modules` (the packaged .app excludes them), the graphs read the truth,
and the bytes equal an unblocked build; the analysis package never imports them."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import brain_analysis_fixtures as F  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
BLOCKED = ("librosa", "torch", "torchaudio", "mlx", "mlx_lm", "pyannote", "faster_whisper", "scipy", "cv2")
BLOCK_CODE = f"import sys\nfor m in {BLOCKED!r}: sys.modules[m] = None\n"

_BUILD = """
import json, sys
from video_ai_editor.brain import graph
jobs = json.loads(sys.argv[1])
out = {name: graph.analyse(sess, sources, gateway=None) for name, (sess, sources) in jobs.items()}
loaded = sorted(m for m in ("librosa", "torch", "mlx", "mlx_lm", "cv2", "scipy") if sys.modules.get(m) is not None)
print(json.dumps({"gids": out, "loaded": loaded}))
"""


def _run(code: str, workdir: Path, *args: str) -> subprocess.CompletedProcess:
    env = {"PYTHONPATH": "src", "PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": os.environ["HOME"],
           "HF_HUB_OFFLINE": "1", "ANTHROPIC_API_KEY": "", "WORKDIR": str(workdir),
           "VAI_LOG_DIR": str(workdir / "logs"), "VAI_BRAIN": "recipes"}
    workdir.mkdir(parents=True, exist_ok=True)
    return subprocess.run([sys.executable, "-c", code, *args], capture_output=True, text=True, cwd=str(REPO), env=env,
                          timeout=900)


def test_analysis_package_imports_without_heavy_deps(tmp_path):
    code = BLOCK_CODE + "\n".join([
        "from video_ai_editor.brain.analysis import pcm, audio, sync, speakers, speech, fillers, semantic, delivery",
        "from video_ai_editor.brain import graph, gateway, digest",
        "from video_ai_editor.ai import diarize",
        "print([m for m in ('librosa','torch','mlx','mlx_lm') if sys.modules.get(m) is not None])",
    ])
    out = _run(code, tmp_path)
    assert out.returncode == 0, out.stderr[-1500:]
    assert out.stdout.strip() == "[]", out.stdout


def _digest_tree(root: Path) -> dict[str, str]:
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(root.rglob("*"))
            if p.is_file() and p.name not in ("timings.jsonl", "refs.json") and "logs" not in p.parts}


def test_graph_builds_with_librosa_torch_mlx_blocked(tmp_path, monkeypatch):
    from video_ai_editor import config
    from video_ai_editor.brain import store
    fx = F.p2_or_skip()
    jobs = {"th": [str(tmp_path / "blocked" / "th"), F.th_sources(fx)],
            "p2": [str(tmp_path / "blocked" / "p2"), F.p2_sources(fx)]}
    blocked = _run(BLOCK_CODE + _BUILD, tmp_path / "blocked" / "work", json.dumps(jobs))
    assert blocked.returncode == 0, blocked.stderr[-2500:]
    b = json.loads(blocked.stdout.strip().splitlines()[-1])
    assert b["loaded"] == [], b
    # the blocked build reads the truth
    monkeypatch.setattr(config, "WORKDIR", tmp_path / "blocked" / "work")
    F.check_th_graph(fx, tmp_path / "blocked" / "th", b["gids"]["th"])
    F.check_p2_graph(fx, tmp_path / "blocked" / "p2", b["gids"]["p2"])
    assert store.current_graph_id(tmp_path / "blocked" / "p2") == b["gids"]["p2"]
    # an unblocked build (librosa importable) writes the same bytes
    jobs_open = {k: [v[0].replace("/blocked/", "/open/"), v[1]] for k, v in jobs.items()}
    opened = _run(_BUILD, tmp_path / "open" / "work", json.dumps(jobs_open))
    assert opened.returncode == 0, opened.stderr[-2500:]
    o = json.loads(opened.stdout.strip().splitlines()[-1])
    assert o["gids"] == b["gids"]
    for name in ("th", "p2"):
        assert _digest_tree(tmp_path / "blocked" / name) == _digest_tree(tmp_path / "open" / name)
    assert _digest_tree(tmp_path / "blocked" / "work" / "analysis") == _digest_tree(tmp_path / "open" / "work" / "analysis")
