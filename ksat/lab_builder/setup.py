"""Resolve packaging-PC tools and safe per-build defaults; never provision keys."""
import os
from pathlib import Path
import re
import shutil
import tempfile
from urllib.parse import urlsplit

from ksat.lab_builder.build import BuildTools
from ksat.lab_builder.controller import BuilderSettings
from ksat.lab_builder.signing import PUBLISHER, THUMBPRINT, SignerSelection


def resolve_settings(saved: BuilderSettings | None = None) -> BuilderSettings:
    local = Path(os.environ.get("LOCALAPPDATA", str(Path.home())))
    programs = [Path(os.environ.get(key, fallback)) for key, fallback in (
        ("ProgramFiles", "C:/Program Files"), ("ProgramFiles(x86)", "C:/Program Files (x86)"))]
    compiler = [local / "Programs/Inno Setup 6/ISCC.exe"]
    compiler += [root / "Inno Setup 6/ISCC.exe" for root in programs]
    signers = list((local / "KSAT Build Tools").glob("*/package/bin/*/x64/signtool.exe"))
    for root in programs:
        signers += list(root.glob("Windows Kits/10/bin/*/x64/signtool.exe"))
    signers.sort(key=lambda p: tuple(int(n) for n in re.findall(r"\d+", p.parent.parent.name)), reverse=True)
    # A maintainer-provisioned location outside AppData works identically from
    # a packaged development app and a normal Explorer-launched builder.
    permanent = Path.home() / "KSAT Build Tools"
    signers.insert(0, permanent / "SignTool/x64/signtool.exe")
    extractors = [permanent / "innoextract670/innoextract.exe",
                  local / "KSAT Build Tools/innoextract670/innoextract.exe",
                  local / "KSAT Lab Package Builder/tools/innoextract.exe",
                  Path(os.environ.get("TEMP", str(local / "Temp"))) / "KSATBuildTools/innoextract670/innoextract.exe"]
    choices = {"iscc": compiler, "signtool": signers, "innoextract": extractors}
    names = {"iscc": "ISCC.exe", "signtool": "signtool.exe", "innoextract": "innoextract.exe"}
    resolved = {}
    for key, candidates in choices.items():
        previous = getattr(saved.tools, key) if saved else None
        if previous is not None and previous.is_absolute() and previous.is_file():
            resolved[key] = previous
            continue
        on_path = shutil.which(names[key])
        if on_path:
            candidates.append(Path(on_path))
        resolved[key] = next((p.resolve() for p in candidates if p.is_absolute() and p.is_file()), Path(names[key]))
    signer = saved.signer if saved else SignerSelection("CurrentUser", THUMBPRINT, PUBLISHER, None, True)
    return BuilderSettings(BuildTools(**resolved), signer)


def validate_tools(tools: BuildTools):
    labels = {"iscc": "Inno Setup 6.7.3", "signtool": "Microsoft SignTool", "innoextract": "compatible innoextract"}
    missing = [label for key, label in labels.items()
               if not getattr(tools, key).is_absolute() or not getattr(tools, key).is_file()]
    if missing:
        raise ValueError("This packaging PC is missing: " + ", ".join(missing) +
                         ". Install the required tool or select its location in Advanced. Your server connection files are not the problem.")


def default_lab_name(url: str) -> str:
    return ("KSAT " + (urlsplit(url).hostname or "Lab"))[:80]


def prepare_output_folder(profile, requested: str) -> Path:
    if requested.strip():
        path = Path(requested).expanduser().absolute()
        if not path.is_dir():
            raise ValueError("The output folder selected in Advanced does not exist.")
        return path
    base = Path.home() / "Downloads/KSAT Lab Installers"
    base.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix=profile.lab_slug + "-", dir=base))
