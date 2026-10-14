"""
Enderun AI v2.0 — PyInstaller Paketleme Scripti
Kullanım:  python build.py
Çıktı:     dist/ klasöründe platform'a özgü çalıştırılabilir
"""
import platform
import subprocess
import sys
from pathlib import Path

ROOT    = Path(__file__).parent
DIST    = ROOT / "dist"
BUILD   = ROOT / "build"
SPEC    = ROOT / "enderun.spec"

# Platform'a göre ek ayarlar
OS = platform.system()  # "Windows", "Darwin", "Linux"

def main():
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--clean",
        "--noconfirm",
        f"--distpath={DIST}",
        f"--workpath={BUILD}",
        "--name=EnderunAI",
        "--onefile",           # Tek dosya
        "--windowed",          # Konsol penceresi açma (Windows/macOS)
        # Veri klasörleri
        f"--add-data=frontend{':' if OS != 'Windows' else ';'}frontend",
        f"--add-data=backend{':' if OS != 'Windows' else ';'}backend",
        # Gizli import'lar
        "--hidden-import=uvicorn.logging",
        "--hidden-import=uvicorn.loops",
        "--hidden-import=uvicorn.loops.auto",
        "--hidden-import=uvicorn.protocols",
        "--hidden-import=uvicorn.protocols.http",
        "--hidden-import=uvicorn.protocols.http.auto",
        "--hidden-import=uvicorn.protocols.websockets",
        "--hidden-import=uvicorn.protocols.websockets.auto",
        "--hidden-import=uvicorn.lifespan",
        "--hidden-import=uvicorn.lifespan.on",
        "--hidden-import=fastapi",
        "--hidden-import=ortools",
        "--hidden-import=openpyxl",
        "--hidden-import=pandas",
        "--hidden-import=webview",
        "app.py",
    ]

    if OS == "Windows":
        # Windows'ta konsol penceresini tamamen gizle
        cmd += ["--version-file=version.txt"] if Path("version.txt").exists() else []
    elif OS == "Darwin":
        cmd += [
            "--osx-bundle-identifier=com.enderun.ai",
            "--target-arch=universal2",  # Apple Silicon + Intel
        ]

    print(f"[Enderun AI Build] Platform: {OS}")
    print(f"[Enderun AI Build] Komut: {' '.join(cmd)}\n")

    result = subprocess.run(cmd, cwd=ROOT)

    if result.returncode == 0:
        cikti = DIST / ("EnderunAI.exe" if OS == "Windows" else
                        "EnderunAI.app" if OS == "Darwin" else "EnderunAI")
        print(f"\n✅ Başarılı! Çıktı: {cikti}")
    else:
        print(f"\n❌ Build başarısız. Hata kodu: {result.returncode}")
        sys.exit(result.returncode)


if __name__ == "__main__":
    main()
