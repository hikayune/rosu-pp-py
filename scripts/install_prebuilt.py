from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

try:
    from _prebuilt_backend import _configured_repo, _release_tag, download_compatible_wheel
except ModuleNotFoundError as error:
    if error.name != "packaging":
        raise
    subprocess.check_call([sys.executable, "-m", "pip", "install", "packaging>=23"])
    from _prebuilt_backend import _configured_repo, _release_tag, download_compatible_wheel


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Instala uma wheel precompilada do rosu-pp-py publicada em uma GitHub Release."
    )
    parser.add_argument(
        "--repo",
        help="Repositório GitHub no formato OWNER/REPO. Por padrão, usa o remote origin.",
    )
    parser.add_argument(
        "--tag",
        help="Tag da release. Por padrão, usa a última release publicada no GitHub.",
    )
    parser.add_argument(
        "--force-reinstall",
        action="store_true",
        help="Reinstala o pacote mesmo que ele já esteja instalado.",
    )
    parser.add_argument(
        "--no-deps",
        action="store_true",
        help="Instala a wheel sem resolver dependências pelo pip.",
    )
    return parser


def install_wheel(wheel_path: Path, force_reinstall: bool, no_deps: bool) -> None:
    command = [sys.executable, "-m", "pip", "install"]
    if force_reinstall:
        command.append("--force-reinstall")
    if no_deps:
        command.append("--no-deps")
    command.append(str(wheel_path))
    subprocess.check_call(command)


def main() -> int:
    args = build_parser().parse_args()
    repo = args.repo or _configured_repo()
    tag = args.tag or _release_tag()

    if not repo:
        print(
            "Não consegui detectar o repositório GitHub. Informe --repo OWNER/REPO ou defina ROSU_PP_PY_PREBUILT_REPO.",
            file=sys.stderr,
        )
        return 2

    with tempfile.TemporaryDirectory() as temp_dir:
        wheel_name = download_compatible_wheel(temp_dir, repo=repo, tag=tag)
        if not wheel_name:
            print(
                f"Nenhuma wheel compatível foi encontrada em {repo} na release {tag}.",
                file=sys.stderr,
            )
            return 1

        wheel_path = Path(temp_dir) / wheel_name
        release = tag or "última release"
        print(f"Instalando {wheel_name} de {repo} ({release})...")
        install_wheel(wheel_path, args.force_reinstall, args.no_deps)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
