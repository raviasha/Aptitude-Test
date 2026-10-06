"""Maintainer-only freeze/sign/inspect pipeline. No PFX or private-key input."""
from __future__ import annotations
import argparse
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path: sys.path.insert(0, str(ROOT))
from ksat.lab_builder import BUILDER_VERSION
from ksat.lab_builder.build import publish_no_replace
from ksat.lab_builder.payload import load_approved_payload, REQUIRED_FILES
from ksat.lab_builder.signing import PUBLISHER, THUMBPRINT, SignerSelection, sign_from_store
from ksat.windows_authenticode import verify_authenticode


def assemble_builder_resources(payload_root: Path, destination: Path) -> Path:
    payload = load_approved_payload(payload_root)
    destination.mkdir(parents=True, exist_ok=False)
    for name in REQUIRED_FILES | {"payload-manifest.json"}:
        shutil.copyfile(payload.root / name, destination / name)
    load_approved_payload(destination)
    return destination / "payload-manifest.json"


def builder_command(root: Path, python: Path, work: Path, resources: Path) -> list[str]:
    return [str(python), "-m", "PyInstaller", "--noconfirm", "--clean", "--onefile", "--windowed",
        "--name", f"KSATLabPackageBuilder-{BUILDER_VERSION}", "--distpath", str(work / "dist"),
        "--workpath", str(work / "work"), "--specpath", str(work / "spec"), "--paths", str(root),
        "--version-file", str(work / "builder.version.txt"), "--hidden-import", "tkinter",
        "--hidden-import", "PyInstaller.archive.readers", "--add-data", str(resources) + os.pathsep + "lab-payload",
        str(root / "lab_package_builder.py")]


def verify_frozen_builder(path: Path, resources: Path):
    from PyInstaller.archive.readers import CArchiveReader
    from ksat.archive_inspection import pyinstaller_payload_manifest
    identity = verify_authenticode(path, PUBLISHER)
    if identity.thumbprint != THUMBPRINT: raise ValueError("Unexpected builder publisher.")
    manifest = pyinstaller_payload_manifest(path)
    if not any("_tkinter.pyd" in name for name in manifest): raise ValueError("Frozen Tk runtime is missing.")
    if not any("_tcl_data" in name for name in manifest): raise ValueError("Frozen Tcl resources are missing.")
    archive = CArchiveReader(str(path))
    embedded = {name.replace("\\", "/"): name for name in archive.toc if name.replace("\\", "/").startswith("lab-payload/")}
    expected = {"lab-payload/" + name for name in REQUIRED_FILES | {"payload-manifest.json"}}
    if set(embedded) != expected: raise ValueError("Builder payload allowlist does not match.")
    for name, archived in embedded.items():
        if archive.extract(archived) != (resources / name.split("/", 1)[1]).read_bytes():
            raise ValueError("Frozen builder payload differs from its approved resources.")
    load_approved_payload(resources)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--payload-root", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--signtool", type=Path, required=True)
    args = parser.parse_args(argv)
    output = args.output_dir.absolute()
    output.mkdir(parents=True, exist_ok=True)
    name = f"KSATLabPackageBuilder-{BUILDER_VERSION}.exe"
    destination = output / name
    checksum = output / f"KSATLabPackageBuilder-{BUILDER_VERSION}.sha256"
    if destination.exists() or checksum.exists(): raise FileExistsError("Builder output already exists.")
    with tempfile.TemporaryDirectory(prefix=".ksat-freeze-", dir=output) as temporary:
        work = Path(temporary)
        resources = work / "lab-payload"
        assemble_builder_resources(args.payload_root, resources)
        (work / "builder.version.txt").write_text("""VSVersionInfo(
ffi=FixedFileInfo(filevers=(2,1,3,0),prodvers=(2,1,3,0),mask=0x3f,flags=0,OS=0x40004,fileType=1,subtype=0,date=(0,0)),
kids=[StringFileInfo([StringTable('040904B0',[
StringStruct('FileDescription','KSAT Lab Package Builder'),StringStruct('FileVersion','2.1.3'),
StringStruct('ProductName','KSAT Lab Package Builder'),StringStruct('ProductVersion','2.1.3')])]),
VarFileInfo([VarStruct('Translation',[1033,1200])])])
""", encoding="utf-8")
        subprocess.run(builder_command(ROOT, Path(sys.executable), work, resources), cwd=ROOT, check=True, timeout=600)
        artifact = work / "dist" / name
        sign_from_store(artifact, SignerSelection("CurrentUser", THUMBPRINT, PUBLISHER, None, True), args.signtool)
        verify_frozen_builder(artifact, resources)
        digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
        receipt = work / "checksum.txt"
        receipt.write_text(f"{digest}  {name}\n", encoding="ascii")
        publish_no_replace(receipt, checksum)
        try: publish_no_replace(artifact, destination)
        except Exception:
            checksum.unlink()
            raise
    print(f"Verified builder: {destination}\nSHA256: {digest}")
    return 0


if __name__ == "__main__": raise SystemExit(main())
