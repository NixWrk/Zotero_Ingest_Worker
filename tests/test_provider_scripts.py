from __future__ import annotations

from pathlib import Path

from zotero_ingest_worker.provider_scripts import provider_script_path


def test_provider_script_path_falls_back_to_workdir_scripts(tmp_path: Path) -> None:
    package_root = tmp_path / "site-packages"
    workdir = tmp_path / "app"
    script = workdir / "scripts" / "providers" / "researchgate_pdf_browser_download.py"
    script.parent.mkdir(parents=True)
    script.write_text("# helper", encoding="utf-8")

    assert (
        provider_script_path(
            "researchgate_pdf_browser_download.py",
            package_root=package_root,
            cwd=workdir,
        )
        == script
    )
